from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from .models import Lot
from .serializers import LotSerializer
from transferts.serializers import TransfertSerializer
from transferts.models import Transfert
from blockchain.service import BlockchainService
from users.permissions import EstAgriculteur, EstExportateur
import qrcode
import io
import base64


class LotListCreateView(APIView):

    def get_permissions(self):
        if self.request.method == 'POST':
            # Seul un agriculteur peut créer un lot
            return [EstAgriculteur()]
        return [IsAuthenticated()]

    def get(self, request):
        """Liste tous les lots de l'utilisateur connecté."""
        lots = Lot.objects.filter(agriculteur=request.user).order_by('-created_at')
        return Response(LotSerializer(lots, many=True).data)

    def post(self, request):
        """Crée un nouveau lot — agriculteur seulement."""
        serializer = LotSerializer(data=request.data)
        if serializer.is_valid():
            lot = serializer.save(agriculteur=request.user)

            blockchain = BlockchainService()
            tx_hash = blockchain.enregistrer_lot(
                lot_id       = str(lot.id),
                hash_donnees = lot.calculer_hash()
            )
            if tx_hash:
                lot.tx_hash = tx_hash
                lot.save()

            qr_code = generer_qr_code(str(lot.id))

            return Response({
                'lot':     LotSerializer(lot).data,
                'qr_code': qr_code,
                'message': '✅ Lot enregistré sur la blockchain !'
                           if tx_hash else '✅ Lot créé (blockchain en attente)'
            }, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class LotDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, lot_id):
        try:
            lot = Lot.objects.get(id=lot_id)
        except Lot.DoesNotExist:
            return Response({'error': 'Lot introuvable'}, status=status.HTTP_404_NOT_FOUND)
        return Response(LotSerializer(lot).data)


class VerifierLotView(APIView):
    """Public — tout le monde peut vérifier."""
    permission_classes = [AllowAny]

    def get(self, request, lot_id):
        try:
            lot = Lot.objects.prefetch_related('transferts').get(id=lot_id)
        except Lot.DoesNotExist:
            return Response({'error': 'Lot introuvable'}, status=404)

        blockchain    = BlockchainService()
        historique_bc = blockchain.get_historique(str(lot.id))
        lot_sur_bc    = blockchain.lot_existe_blockchain(str(lot.id))
        transferts    = lot.transferts.all().order_by('date_transfert')

        return Response({
            'lot':        LotSerializer(lot).data,
            'transferts': TransfertSerializer(transferts, many=True).data,
            'blockchain': {
                'tx_hash':           lot.tx_hash or 'Non enregistré',
                'enregistre_sur_bc': lot_sur_bc,
                'historique':        historique_bc,
                'verification':      '✅ Vérifié sur la blockchain'
                                     if lot_sur_bc else '⏳ En attente',
            },
            'eudr_conforme': lot_sur_bc and lot.tx_hash != ''
        })


class ExporterLotView(APIView):
    """Exportateur seulement."""
    permission_classes = [EstExportateur]

    def post(self, request, lot_id):
        try:
            lot = Lot.objects.get(id=lot_id)
        except Lot.DoesNotExist:
            return Response({'error': 'Lot introuvable'}, status=status.HTTP_404_NOT_FOUND)

        if lot.statut != 'en_transit':
            return Response(
                {'error': f'Le lot est en statut "{lot.statut}", pas en transit'},
                status=status.HTTP_400_BAD_REQUEST
            )

        transfert = Transfert.objects.create(
            lot           = lot,
            expediteur    = request.user,
            destinataire  = request.user,
            etape         = 'exportateur_europe',
            poids_verifie = request.data.get('poids_verifie', lot.poids_kg),
            notes         = request.data.get('notes', '')
        )

        blockchain = BlockchainService()
        tx_hash = blockchain.enregistrer_transfert(
            lot_id  = str(lot.id),
            etape   = 'exportateur_europe',
            user_id = request.user.id
        )
        if tx_hash:
            transfert.tx_hash = tx_hash
            transfert.save()

        lot.statut = 'exporte'
        lot.save()

        return Response({
            'transfert':       TransfertSerializer(transfert).data,
            'lot':             LotSerializer(lot).data,
            'certificat_eudr': f'http://localhost:8000/api/lots/{lot_id}/verify/',
            'message':         '🚢 Lot exporté — Certificat EUDR généré !'
        }, status=status.HTTP_200_OK)


def generer_qr_code(lot_id: str) -> str:
    url    = f"http://localhost:8000/api/lots/{lot_id}/verify/"
    qr     = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(url)
    qr.make(fit=True)
    img    = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)
    return f"data:image/png;base64,{base64.b64encode(buffer.getvalue()).decode()}"


class ScannerLotView(APIView):
    """
    La coopérative/transformateur/exportateur scanne
    le QR code pour voir les infos avant de confirmer.
    Accessible à tous les acteurs connectés.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, lot_id):
        try:
            lot = Lot.objects.prefetch_related('transferts').get(id=lot_id)
        except Lot.DoesNotExist:
            return Response({'error': 'Lot introuvable'}, status=404)

        transferts = lot.transferts.all().order_by('date_transfert')

        # Déterminer la prochaine étape attendue
        etapes_faites = [t.etape for t in transferts]
        prochaine_etape = determiner_prochaine_etape(etapes_faites)

        # Vérifier si cet acteur peut recevoir ce lot
        peut_recevoir = verifier_peut_recevoir(request.user.role, etapes_faites)

        return Response({
            'lot': LotSerializer(lot).data,
            'historique_transferts': TransfertSerializer(transferts, many=True).data,
            'prochaine_etape': prochaine_etape,
            'peut_recevoir': peut_recevoir,
            'message_action': generer_message_action(peut_recevoir, prochaine_etape, request.user.role)
        })


def determiner_prochaine_etape(etapes_faites: list) -> str:
    """Retourne la prochaine étape logique."""
    ordre = [
        'ferme_cooperative',
        'cooperative_transformateur',
        'transformateur_exportateur',
        'exportateur_europe',
    ]
    for etape in ordre:
        if etape not in etapes_faites:
            return etape
    return 'complete'


def verifier_peut_recevoir(role: str, etapes_faites: list) -> bool:
    """Vérifie si cet acteur peut recevoir le lot à cette étape."""
    prochaine = determiner_prochaine_etape(etapes_faites)
    mapping = {
        'ferme_cooperative':          'cooperative',
        'cooperative_transformateur': 'transformateur',
        'transformateur_exportateur': 'exportateur',
        'exportateur_europe':         'exportateur',
    }
    return mapping.get(prochaine) == role


def generer_message_action(peut_recevoir: bool, prochaine_etape: str, role: str) -> str:
    if prochaine_etape == 'complete':
        return '✅ Ce lot a déjà été exporté vers l\'Europe'
    if peut_recevoir:
        return f'✅ Vous pouvez confirmer la réception de ce lot'
    return f'⛔ Ce lot n\'est pas destiné à votre étape pour le moment'



class ConfirmerReceptionView(APIView):
    """
    La coopérative/transformateur/exportateur confirme
    la réception d'un lot après avoir scanné le QR code.
    """
    permission_classes = [IsAuthenticated]

    # Mapping rôle → étape qu'il peut confirmer
    ROLE_ETAPE = {
        'cooperative':    'ferme_cooperative',
        'transformateur': 'cooperative_transformateur',
        'exportateur':    'transformateur_exportateur',
    }

    def post(self, request, lot_id):
        role = request.user.role

        # Vérifier que le rôle peut confirmer une réception
        if role not in self.ROLE_ETAPE:
            return Response(
                {'error': f'Le rôle "{role}" ne peut pas confirmer une réception'},
                status=status.HTTP_403_FORBIDDEN
            )

        try:
            lot = Lot.objects.prefetch_related('transferts').get(id=lot_id)
        except Lot.DoesNotExist:
            return Response({'error': 'Lot introuvable'}, status=404)

        # Vérifier que c'est bien la bonne étape
        etapes_faites  = [t.etape for t in lot.transferts.all()]
        etape_attendue = self.ROLE_ETAPE[role]

        if etape_attendue in etapes_faites:
            return Response(
                {'error': f'Cette étape "{etape_attendue}" a déjà été confirmée'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Vérifier l'ordre logique
        ordre = [
            'ferme_cooperative',
            'cooperative_transformateur',
            'transformateur_exportateur',
        ]
        index_attendu = ordre.index(etape_attendue)
        if index_attendu > 0:
            etape_precedente = ordre[index_attendu - 1]
            if etape_precedente not in etapes_faites:
                return Response(
                    {'error': f'L\'étape précédente "{etape_precedente}" n\'est pas encore confirmée'},
                    status=status.HTTP_400_BAD_REQUEST
                )

        # Trouver l'expéditeur (le propriétaire actuel du lot)
        if etapes_faites:
            dernier_transfert = lot.transferts.order_by('date_transfert').last()
            expediteur = dernier_transfert.destinataire
        else:
            expediteur = lot.agriculteur

        # Créer le transfert
        transfert = Transfert.objects.create(
            lot           = lot,
            expediteur    = expediteur,
            destinataire  = request.user,
            etape         = etape_attendue,
            poids_verifie = request.data.get('poids_verifie', lot.poids_kg),
            notes         = request.data.get('notes', '')
        )

        # Enregistrer sur la blockchain
        blockchain = BlockchainService()
        tx_hash = blockchain.enregistrer_transfert(
            lot_id  = str(lot.id),
            etape   = etape_attendue,
            user_id = request.user.id
        )
        if tx_hash:
            transfert.tx_hash = tx_hash
            transfert.save()

        # Mettre à jour le statut du lot
        lot.statut = 'en_transit'
        lot.save()

        return Response({
            'transfert': TransfertSerializer(transfert).data,
            'lot':       LotSerializer(lot).data,
            'message':   f'✅ Réception confirmée — étape "{etape_attendue}" enregistrée sur la blockchain !'
                         if tx_hash else f'✅ Réception confirmée — étape "{etape_attendue}"'
        }, status=status.HTTP_201_CREATED)

from django.http import HttpResponse
from verification.pdf_generator import generer_certificat_eudr

class CertificatEUDRView(APIView):
    """Génère et télécharge le certificat PDF de traçabilité."""
    permission_classes = [AllowAny]

    def get(self, request, lot_id):
        try:
            lot = Lot.objects.prefetch_related('transferts').get(id=lot_id)
        except Lot.DoesNotExist:
            return Response({'error': 'Lot introuvable'}, status=404)

        transferts    = lot.transferts.all().order_by('date_transfert')
        blockchain    = BlockchainService()
        blockchain_data = {
            'enregistre_sur_bc': blockchain.lot_existe_blockchain(str(lot.id)),
            'tx_hash':           lot.tx_hash or '',
        }

        # Générer le PDF
        pdf_bytes = generer_certificat_eudr(lot, transferts, blockchain_data)

        # Retourner le PDF en téléchargement
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="certificat_eudr_{lot.id}.pdf"'
        return response