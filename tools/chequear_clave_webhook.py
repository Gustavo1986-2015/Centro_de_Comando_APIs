"""
Chequeo de la clave secreta de un webhook firmado, SIN mostrarla.

Para cuando el hub rechaza con "la firma no coincide". La causa más común es
la clave mal pegada en el panel: un espacio, un salto de línea, un caracter
invisible. Este script lee la clave que el hub tiene guardada y dice si tiene
algo raro, comparándola contra la que ves en el proveedor.

La clave nunca se imprime completa ni sale de tu máquina.

Uso, desde la raíz del proyecto, con el hub detenido o andando (da igual):
    python tools/chequear_clave_webhook.py tive prod
"""
import sys
import unicodedata

from dotenv import load_dotenv

# El hub carga el .env al arrancar; acá hay que hacerlo explícito para que la
# llave de cifrado esté disponible y se pueda leer la clave guardada.
load_dotenv()

sys.path.insert(0, ".")

from app.core.crypto import decrypt  # noqa: E402
from app.database import get_session  # noqa: E402
from app.models.config_models import ProviderConfig  # noqa: E402


def _invisibles(texto: str) -> list[str]:
    """Caracteres que no se ven pero cambian el HMAC."""
    hallados = []
    for i, c in enumerate(texto):
        if c.isspace() or unicodedata.category(c) in ("Cf", "Cc", "Zs"):
            hallados.append(f"posición {i}: {unicodedata.name(c, repr(c))}")
    return hallados


def main():
    if len(sys.argv) != 3:
        print("Uso: python tools/chequear_clave_webhook.py <proveedor> <entorno>")
        sys.exit(2)
    proveedor, entorno = sys.argv[1], sys.argv[2]

    db = get_session("system_config", "global")
    try:
        conf = db.query(ProviderConfig).filter(
            ProviderConfig.provider_name.ilike(proveedor),
            ProviderConfig.env == entorno,
        ).first()
    finally:
        db.close()

    if not conf:
        print(f"No existe {proveedor}/{entorno} en la configuración.")
        sys.exit(1)
    if not conf.webhook_auth_secret_enc:
        print(f"{proveedor}/{entorno} NO tiene clave guardada. Cargala en el panel.")
        sys.exit(1)

    clave = decrypt(conf.webhook_auth_secret_enc)
    if not clave:
        print("No se pudo leer la clave guardada. ¿Está MASTER_ENC_KEY en el .env?")
        sys.exit(1)

    print(f"Integración:       {proveedor}/{entorno}")
    print(f"Modo de firma:     {conf.webhook_auth_config}")
    print(f"Largo de la clave: {len(clave)} caracteres")
    # Solo los extremos, para poder compararla a ojo con la del proveedor.
    if len(clave) >= 8:
        print(f"Empieza con:       {clave[:2]}…   Termina con: …{clave[-2:]}")

    problemas = _invisibles(clave)
    if clave != clave.strip():
        problemas.insert(0, "tiene espacios o saltos de línea al principio o al final")
    if problemas:
        print("\nPROBLEMAS ENCONTRADOS — explican el rechazo:")
        for p in problemas:
            print(f"   · {p}")
        print("\nVolvé a pegar la clave en el panel, sin espacios, y guardá.")
    else:
        print("\nLa clave no tiene caracteres invisibles ni espacios.")
        print("Compará el LARGO y los extremos con la clave que muestra el proveedor.")
        print("Si coinciden, el problema no es la clave: avisá con este resultado.")


if __name__ == "__main__":
    main()
