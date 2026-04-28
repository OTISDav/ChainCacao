from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/',           admin.site.urls),
    path('api/auth/',        include('users.urls')),
    path('api/lots/',        include('lots.urls')),
    path('api/transferts/',  include('transferts.urls')),
]