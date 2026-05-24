from app.database import engine, Base
from app.models.db_models import NormalizedRCEvent
from app.schemas.canonical import RCCanonicalModel

def test_creation():
    # 1. Crear tablas en SQLite
    Base.metadata.create_all(bind=engine)
    print("Base de datos y tablas creadas exitosamente.")

    # 2. Testear esquema Pydantic
    mock_data = {
        "chassis_number": "SCHMITZ-12345",
        "latitude": 19.4326,
        "longitude": -99.1332,
        "speed": 65.5,
        "code": "1"
    }
    canonical = RCCanonicalModel(**mock_data)
    print(f"Esquema Pydantic validado: {canonical.chassis_number}, Lat: {canonical.latitude}")

if __name__ == "__main__":
    test_creation()
