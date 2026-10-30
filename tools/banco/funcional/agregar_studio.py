"""Dentro de una copia funcional: agrega una integración del Studio cuyo mapeo
espera el campo de patente "placa" (los payloads de prueba no lo traen)."""
import os
import sys

sys.path.insert(0, os.getcwd())
from app.core.crypto import encrypt  # noqa: E402
from app.database import get_session  # noqa: E402
from app.models.config_models import ProviderConfig  # noqa: E402

db = get_session("system_config", "global")
if not db.query(ProviderConfig).filter_by(provider_name="studio", env="prod").first():
    db.add(ProviderConfig(provider_name="studio", env="prod", provider_type="push", is_active=True, use_mock=True,
                          rc_user="AC_avl_Studio", webhook_auth_secret_enc=encrypt("clave-studio"),
                          mapping_schema={"base_mapping": {"chassis_number": "placa", "latitude": "lat",
                                                           "longitude": "lon", "date": "fecha"}}))
    db.commit()
db.close()
print("studio/prod listo")
