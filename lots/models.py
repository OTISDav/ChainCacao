import uuid
import hashlib
from django.db import models
from users.models import User


class Lot(models.Model):
    ESPECES = [
        ('forastero',  'Forastero'),
        ('trinitario', 'Trinitario'),
        ('criollo',    'Criollo'),
        ('arabica',    'Arabica'),
        ('robusta',    'Robusta'),
    ]

    STATUTS = [
        ('cree',       'Créé'),
        ('en_transit', 'En transit'),
        ('certifie',   'Certifié'),
        ('exporte',    'Exporté'),
    ]

    id            = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agriculteur   = models.ForeignKey(User, on_delete=models.PROTECT, related_name='lots')
    espece        = models.CharField(max_length=20, choices=ESPECES)
    poids_kg      = models.FloatField()
    gps_latitude  = models.FloatField()
    gps_longitude = models.FloatField()
    date_recolte  = models.DateField()
    photo         = models.ImageField(upload_to='lots/', blank=True, null=True)
    notes         = models.TextField(blank=True)
    statut        = models.CharField(max_length=20, choices=STATUTS, default='cree')

    qr_code = models.TextField(blank=True, null=True)



    # Données blockchain (remplies après enregistrement)
    tx_hash       = models.CharField(max_length=100, blank=True)
    block_number  = models.IntegerField(null=True, blank=True)

    created_at    = models.DateTimeField(auto_now_add=True)
    updated_at    = models.DateTimeField(auto_now=True)

    def calculer_hash(self):
        """Génère un hash unique des données du lot pour la blockchain."""
        data = f"{self.id}{self.gps_latitude}{self.gps_longitude}{self.poids_kg}{self.date_recolte}{self.agriculteur_id}"
        return hashlib.sha256(data.encode()).hexdigest()

    def __str__(self):
        return f"Lot {self.id} — {self.espece} {self.poids_kg}kg"