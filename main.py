import csv
import hashlib
import hmac
import os
import re
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo  # En Windows: pip install tzdata

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Carga las variables desde el archivo .env que está junto a main.py
# (si python-dotenv no está instalado, usa las variables del terminal)
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(BASE_DIR, ".env"), override=True)
except ImportError:
    print("AVISO: python-dotenv no está instalado (pip install python-dotenv)")

app = FastAPI(title="Aromas Envios - Carrier API", version="2.1")

# --- Configuración por variables de entorno (NO dejar secretos en el código) ---
CLIENT_ID = os.getenv("TN_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("TN_CLIENT_SECRET", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")  # https://xxxx.ngrok-free.dev
# Región desde donde despachas (para la fila "Misma Región" del CSV).
# Si la dejas vacía se usa la región de origen configurada en Tienda Nube.
ORIGEN_REGION = os.getenv("ORIGEN_REGION", "").strip()
# Divisor del peso volumétrico por defecto (largo x ancho x alto en cm / 4000)
DIVISOR_VOLUMETRICO = float(os.getenv("DIVISOR_VOLUMETRICO", "4000"))
# Divisor volumétrico parametrizado por courier
DIVISOR_VOLUMETRICO_POR_COURIER = {
    "blue_express": float(os.getenv("DIVISOR_VOLUMETRICO_BLUE_EXPRESS", str(DIVISOR_VOLUMETRICO))),
    "starken": float(os.getenv("DIVISOR_VOLUMETRICO_STARKEN", "4000")),
    "chilexpress": float(os.getenv("DIVISOR_VOLUMETRICO_CHILEXPRESS", "5000")),
}
# Fallbacks obligatorios cuando Tiendanube envía productos sin peso o sin dimensiones
FALLBACK_GRAMOS_UNIDAD = 450.0  # 0.45 kg por perfume
FALLBACK_LARGO_CM = 15.0
FALLBACK_ANCHO_CM = 10.0
FALLBACK_ALTO_CM = 8.0
FALLBACK_VOLUMEN_CM3 = FALLBACK_LARGO_CM * FALLBACK_ANCHO_CM * FALLBACK_ALTO_CM  # 1200 cm³

# 1 = el costo de embalaje (caja, burbuja, etiqueta...) se suma al precio que paga el cliente
COBRAR_EMBALAJE = os.getenv("COBRAR_EMBALAJE", "1").strip() == "1"
# Máximo de bultos por pedido; sobre eso se responde 422 (pedido a coordinar aparte)
MAX_PAQUETES = int(os.getenv("MAX_PAQUETES", "10"))


def _parse_couriers_activos(valor) -> list[str]:
    if isinstance(valor, (list, tuple, set)):
        items = [str(c).strip().lower() for c in valor]
    else:
        items = [c.strip().lower() for c in str(valor or "").split(",")]
    activos = [c for c in items if c]
    return activos or ["blue_express"]


# Couriers habilitados en checkout (por defecto solo blue_express para no romper checkout actual)
COURIERS_ACTIVOS = _parse_couriers_activos(os.getenv("COURIERS_ACTIVOS", "blue_express"))


def obtener_couriers_activos(couriers_activos=None) -> list[str]:
    """Devuelve la lista normalizada de couriers activos.
    Lee COURIERS_ACTIVOS desde el entorno dinámicamente si está definido."""
    if couriers_activos is not None:
        return _parse_couriers_activos(couriers_activos)
    env_val = os.getenv("COURIERS_ACTIVOS")
    if env_val is not None and env_val.strip():
        return _parse_couriers_activos(env_val)
    return _parse_couriers_activos(COURIERS_ACTIVOS)


def obtener_divisor_volumetrico(courier: str = "blue_express") -> float:
    """Devuelve el divisor volumétrico configurado para un courier (default 4000)."""
    c = (courier or "blue_express").strip().lower()
    return float(DIVISOR_VOLUMETRICO_POR_COURIER.get(c, DIVISOR_VOLUMETRICO))


print(
    f"DEBUG credenciales: client_id={CLIENT_ID!r}, largo_secret={len(CLIENT_SECRET)}, "
    f"public_url={PUBLIC_URL!r}, couriers_activos={obtener_couriers_activos()!r}"
)

API_BASE = "https://api.tiendanube.com/v1"
TOKEN_URL = "https://www.tiendanube.com/apps/authorize/token"
CARRIER_NAME = "Blue Express"
OPTION_CODE = "blue_express_standard"      # Blue Express domicilio (debe coincidir EXACTO con "code")
PUNTO_CODE = "blue_express_punto"          # Blue Express retiro en punto
STARKEN_DOMICILIO_CODE = "starken_domicilio"
STARKEN_AGENCIA_CODE = "starken_agencia"
CHILEXPRESS_DOMICILIO_CODE = "chilexpress_domicilio"
CHILEXPRESS_SUCURSAL_CODE = "chilexpress_sucursal"

# Tarifas provisorias cuando no se puede determinar la región (p. ej. en el carrito)
TARIFA_DEFECTO = 4500
TARIFA_DEFECTO_PUNTO = int(os.getenv("TARIFA_DEFECTO_PUNTO", str(TARIFA_DEFECTO)))
TARIFA_DEFECTO_STARKEN = int(os.getenv("TARIFA_DEFECTO_STARKEN", "4800"))
TARIFA_DEFECTO_STARKEN_AGENCIA = int(os.getenv("TARIFA_DEFECTO_STARKEN_AGENCIA", "4000"))
TARIFA_DEFECTO_CHILEXPRESS = int(os.getenv("TARIFA_DEFECTO_CHILEXPRESS", "5200"))
TARIFA_DEFECTO_CHILEXPRESS_SUCURSAL = int(os.getenv("TARIFA_DEFECTO_CHILEXPRESS_SUCURSAL", "4400"))

CATALOGO_SERVICIOS = [
    {
        "courier": "blue_express",
        "servicio": "domicilio",
        "code": OPTION_CODE,
        "nombre": "Blue Express - Envío a domicilio",
        "tarifa_defecto": TARIFA_DEFECTO,
        "phone_required": False,
        "min_dias": 2,
        "max_dias": 3,
    },
    {
        "courier": "blue_express",
        "servicio": "punto",
        "code": PUNTO_CODE,
        "nombre": "Blue Express - Retiro en punto",
        "tarifa_defecto": TARIFA_DEFECTO_PUNTO,
        "phone_required": True,
        "min_dias": 2,
        "max_dias": 3,
    },
    {
        "courier": "starken",
        "servicio": "domicilio",
        "code": STARKEN_DOMICILIO_CODE,
        "nombre": "Starken - Envío a domicilio",
        "tarifa_defecto": TARIFA_DEFECTO_STARKEN,
        "phone_required": False,
        "min_dias": 2,
        "max_dias": 4,
    },
    {
        "courier": "starken",
        "servicio": "agencia",
        "code": STARKEN_AGENCIA_CODE,
        "nombre": "Starken - Retiro en agencia",
        "tarifa_defecto": TARIFA_DEFECTO_STARKEN_AGENCIA,
        "phone_required": True,
        "min_dias": 2,
        "max_dias": 4,
    },
    {
        "courier": "chilexpress",
        "servicio": "domicilio",
        "code": CHILEXPRESS_DOMICILIO_CODE,
        "nombre": "Chilexpress - Envío a domicilio",
        "tarifa_defecto": TARIFA_DEFECTO_CHILEXPRESS,
        "phone_required": False,
        "min_dias": 1,
        "max_dias": 3,
    },
    {
        "courier": "chilexpress",
        "servicio": "sucursal",
        "code": CHILEXPRESS_SUCURSAL_CODE,
        "nombre": "Chilexpress - Retiro en sucursal",
        "tarifa_defecto": TARIFA_DEFECTO_CHILEXPRESS_SUCURSAL,
        "phone_required": True,
        "min_dias": 1,
        "max_dias": 3,
    },
]


def servicios_activos(couriers_activos=None) -> list[dict]:
    """Devuelve solo los servicios del catálogo pertenecientes a COURIERS_ACTIVOS."""
    activos = set(obtener_couriers_activos(couriers_activos))
    return [s for s in CATALOGO_SERVICIOS if s["courier"] in activos]


# Alias de compatibilidad hacia atrás
OPCIONES = [(s["code"], s["nombre"]) for s in CATALOGO_SERVICIOS if s["courier"] == "blue_express"]
TZ = ZoneInfo("America/Santiago")

# Tallas de Blue Express y peso tarifario máximo (kg) de cada una.
# XS: 0-0.5 | S: 0.5-3 | M: 3-6 | L: 6-16 | XL: 16-25. Sobre 25 kg un bulto no es despachable.
TALLAS = [
    ("xs", 0.5),
    ("s", 3.0),
    ("m", 6.0),
    ("l", 16.0),
    ("xl", 25.0),
]


# ------------------------------------------------------------- Utilidades CSV
def reparar(s: str) -> str:
    """Corrige texto mal codificado: 'ValparaÃ­so' -> 'Valparaíso'."""
    if "Ã" in s or "Â" in s:
        try:
            return s.encode("cp1252").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return s


def clave(s: str) -> str:
    """Normaliza nombres de región: sin tildes, sin símbolos y sin la palabra 'región'.
    'Bío - Bío' == 'Biobío', 'Región Metropolitana' == 'Metropolitana'."""
    s = unicodedata.normalize("NFD", reparar(s or "").lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-z]", "", s)
    return s.replace("region", "")


def _num(v) -> float:
    """Pesos chilenos: '4.500' o '$4500' -> 4500."""
    return float(str(v).replace("$", "").replace(".", "").replace(",", ".").strip())


def _dec(v) -> float:
    """Decimales (kg, cm): '0.3' o '0,3' -> 0.3."""
    return float(str(v).replace(",", ".").strip())


def leer_csv(nombre: str):
    """Lee un CSV junto a main.py (UTF-8, con BOM o ANSI de Excel; separador , o ;).
    Ignora líneas vacías y las que empiezan con #. Devuelve None si no existe."""
    ruta = os.path.join(BASE_DIR, nombre)
    if not os.path.exists(ruta):
        return None
    with open(ruta, "rb") as fb:
        crudo = fb.read()
    try:
        texto = crudo.decode("utf-8-sig")
    except UnicodeDecodeError:
        texto = crudo.decode("cp1252")
    lineas = [l for l in texto.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    if not lineas:
        return []
    delim = ";" if lineas[0].count(";") > lineas[0].count(",") else ","
    return list(csv.DictReader(lineas, delimiter=delim))


# ---------------------------------------------------------------- Tarifas CSV
_tarifas = None


def cargar_tarifas(force_reload: bool = False) -> dict:
    """tarifas.csv -> {(courier, region, tipo_servicio): {"xs": precio, "s": precio, ...}}
    Compatible hacia atrás: si una fila omite la columna 'courier', asume 'blue_express'."""
    global _tarifas
    if _tarifas is not None and not force_reload:
        return _tarifas
    _tarifas = {}
    filas = leer_csv("tarifas.csv")
    if filas is None:
        print("AVISO: no existe tarifas.csv, se usarán las tarifas por defecto")
        return _tarifas
    for row in filas:
        try:
            courier = (row.get("courier") or "blue_express").strip().lower() or "blue_express"
            servicio = row["tipo_servicio"].strip().lower()
            precios = {t: _num(row[t]) for t, _ in TALLAS}
            _tarifas[(courier, clave(row["region"]), servicio)] = precios
        except Exception as e:
            print(f"Fila inválida en tarifas.csv {row}: {e!r}")
    print(f"Tarifas cargadas: {len(_tarifas)} filas (courier + región + servicio)")
    return _tarifas


def talla_para(kg: float):
    """Talla según el peso tarifario, o None si excede el máximo (25 kg)."""
    for nombre, maximo in TALLAS:
        if kg <= maximo:
            return nombre
    return None


def buscar_precios(provincia: str, origen: str, servicio: str = "domicilio", courier: str = "blue_express"):
    """Devuelve (región encontrada, precios por talla) o (None, None) para el courier y servicio indicados."""
    tarifas = cargar_tarifas()
    c = (courier or "blue_express").strip().lower() or "blue_express"
    serv = (servicio or "domicilio").strip().lower()
    k = clave(provincia)
    if not k:
        return None, None
    if origen and k == clave(origen) and (c, "misma", serv) in tarifas:
        return "misma región", tarifas[(c, "misma", serv)]
    if (c, k, serv) in tarifas:
        return k, tarifas[(c, k, serv)]
    for (cour, reg, s_row), precios in tarifas.items():  # coincidencia parcial (p. ej. "O'Higgins")
        if cour == c and s_row == serv and reg != "misma" and (k in reg or reg in k):
            return reg, precios
    return None, None


# ------------------------------------------------------------ Cajas (Opción C)
_cajas = None


def cargar_cajas(force_reload: bool = False) -> list:
    """cajas.csv -> lista ordenada por capacidad:
    max_unidades, largo_cm, ancho_cm, alto_cm, peso_embalaje_kg, costo_embalaje, nota"""
    global _cajas
    if _cajas is not None and not force_reload:
        return _cajas
    _cajas = []
    filas = leer_csv("cajas.csv")
    if filas is None:
        print("AVISO: no existe cajas.csv; se usará solo el peso y las medidas de los productos")
        return _cajas
    for row in filas:
        try:
            _cajas.append(
                {
                    "max_unidades": int(_dec(row["max_unidades"])),
                    "largo": _dec(row["largo_cm"]),
                    "ancho": _dec(row["ancho_cm"]),
                    "alto": _dec(row["alto_cm"]),
                    "peso_embalaje": _dec(row.get("peso_embalaje_kg") or 0),
                    "costo_embalaje": _num(row.get("costo_embalaje") or 0),
                    "nota": (row.get("nota") or "").strip(),
                }
            )
        except Exception as e:
            print(f"Fila inválida en cajas.csv {row}: {e!r}")
    _cajas.sort(key=lambda c: c["max_unidades"])
    print(f"Cajas cargadas: {len(_cajas)}")
    if any("ejemplo" in c["nota"].lower() for c in _cajas):
        print("AVISO: cajas.csv contiene valores de EJEMPLO. Reemplázalos por tus cajas reales.")
    return _cajas


def planificar_paquetes(items: list, divisor_volumetrico: float = None, courier: str = None) -> list:
    """Reparte las unidades del carrito en cajas y calcula el peso tarificable de cada bulto:
        peso_tarificable = max(
            peso_real_bulto (productos + embalaje),
            peso_volumetrico_caja (largo x ancho x alto / divisor_volumetrico),
            peso_volumetrico_productos (volumen productos / divisor_volumetrico),
        )
    Aplica fallbacks obligatorios por ítem si llegan sin peso (450.0 g) o sin dimensiones (15x10x8 cm = 1200 cm³).
    Lanza ValueError si el pedido no se puede despachar."""
    if divisor_volumetrico is not None:
        divisor = float(divisor_volumetrico)
    elif courier:
        divisor = obtener_divisor_volumetrico(courier)
    else:
        divisor = DIVISOR_VOLUMETRICO
    if divisor <= 0:
        divisor = 4000.0

    unidades = 0
    gramos = 0.0
    vol_cm3 = 0.0
    fallback_peso_usado = False
    fallback_dim_usado = False

    if not items:
        unidades = 1
        gramos = FALLBACK_GRAMOS_UNIDAD
        vol_cm3 = FALLBACK_VOLUMEN_CM3
        fallback_peso_usado = True
        fallback_dim_usado = True
    else:
        for i in items:
            try:
                qty = int(i.get("quantity") or 1)
            except (TypeError, ValueError):
                qty = 1
            if qty <= 0:
                qty = 1

            try:
                g_item = float(i.get("grams") or 0)
            except (TypeError, ValueError):
                g_item = 0.0
            if g_item <= 0:
                g_item = FALLBACK_GRAMOS_UNIDAD
                fallback_peso_usado = True

            d = i.get("dimensions") or {}
            try:
                w = float(d.get("width") or 0)
                h = float(d.get("height") or 0)
                p = float(d.get("depth") or 0)
            except (TypeError, ValueError):
                w, h, p = 0.0, 0.0, 0.0
            if w <= 0 or h <= 0 or p <= 0:
                vol_item = FALLBACK_VOLUMEN_CM3
                fallback_dim_usado = True
            else:
                vol_item = w * h * p

            unidades += qty
            gramos += g_item * qty
            vol_cm3 += vol_item * qty

    if fallback_peso_usado:
        print(f"AVISO: producto(s) sin peso en Tiendanube -> fallback aplicado ({FALLBACK_GRAMOS_UNIDAD:g} g/u)")
    if fallback_dim_usado:
        print(
            f"AVISO: producto(s) sin dimensiones en Tiendanube -> fallback aplicado "
            f"({FALLBACK_LARGO_CM:g}x{FALLBACK_ANCHO_CM:g}x{FALLBACK_ALTO_CM:g} cm = {FALLBACK_VOLUMEN_CM3:g} cm³/u)"
        )

    g_unidad = gramos / unidades
    vol_unidad = vol_cm3 / unidades

    cajas = cargar_cajas()
    if cajas:
        capacidad = cajas[-1]["max_unidades"]
        reparto = [capacidad] * (unidades // capacidad)
        if unidades % capacidad:
            reparto.append(unidades % capacidad)
    else:
        reparto = [unidades]  # sin cajas.csv: un solo bulto

    if len(reparto) > MAX_PAQUETES:
        raise ValueError(f"{unidades} unidades requieren {len(reparto)} bultos (máximo {MAX_PAQUETES})")

    paquetes = []
    for u in reparto:
        caja = next((c for c in cajas if c["max_unidades"] >= u), None)
        peso_real = u * g_unidad / 1000.0 + (caja["peso_embalaje"] if caja else 0.0)
        vol_caja = (caja["largo"] * caja["ancho"] * caja["alto"] / divisor) if caja else 0.0
        vol_prod = u * vol_unidad / divisor
        tarifario = max(peso_real, vol_caja, vol_prod)
        talla = talla_para(tarifario)
        if talla is None:
            raise ValueError(f"un bulto de {u} unidades pesa {tarifario:.1f} kg (máximo {TALLAS[-1][1]} kg)")
        paquetes.append(
            {
                "unidades": u,
                "caja": f"{caja['largo']:g}x{caja['ancho']:g}x{caja['alto']:g}" if caja else "sin caja",
                "peso_real": peso_real,
                "peso_vol_caja": vol_caja,
                "peso_vol_prod": vol_prod,
                "tarifario": tarifario,
                "peso_tarificable": tarifario,
                "talla": talla,
                "costo_embalaje": caja["costo_embalaje"] if caja else 0,
                "divisor_volumetrico": divisor,
                "fallback_peso_aplicado": fallback_peso_usado,
                "fallback_dim_aplicado": fallback_dim_usado,
            }
        )
    return paquetes


# ---------------------------------------------------------------------- Rutas
async def _sincronizar_carrier_y_opciones(
    client: httpx.AsyncClient,
    store_id: str,
    access_token: str,
    couriers_activos=None,
    actualizar_carrier: bool = True,
) -> dict:
    """Registra/actualiza el carrier en Tiendanube y crea las opciones faltantes
    de los couriers activos sin duplicar las ya existentes."""
    headers = {
        "Authentication": f"bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": "Aromas App (contacto@aromasdelpacifico.cl)",  # solo ASCII
    }
    url = f"{API_BASE}/{store_id}/shipping_carriers"
    callback_url = f"{PUBLIC_URL}/carrier/calculate" if PUBLIC_URL else ""

    get_res = await client.get(url, headers=headers)
    print(f"LISTA CARRIERS: {get_res.status_code} - {get_res.text[:300]}")
    if get_res.status_code != 200:
        raise HTTPException(status_code=502, detail=f"No se pudo listar carriers: {get_res.text}")

    carriers_lista = get_res.json() or []
    existente = next(
        (
            c
            for c in carriers_lista
            if c.get("name") == CARRIER_NAME
            or (callback_url and c.get("callback_url") == callback_url)
        ),
        None,
    )

    if existente:
        carrier_id = existente["id"]
        if actualizar_carrier and callback_url:
            carrier = {
                "name": CARRIER_NAME,
                "callback_url": callback_url,
                "types": "ship",
                "active": True,
            }
            res = await client.put(f"{url}/{carrier_id}", json=carrier, headers=headers)
            print(f"ACTUALIZACION CARRIER: {res.status_code} - {res.text}")
            if res.status_code >= 400:
                raise HTTPException(status_code=502, detail=f"No se pudo actualizar el carrier: {res.text}")
    else:
        if not callback_url:
            raise HTTPException(
                status_code=500,
                detail="Falta variable PUBLIC_URL para crear el carrier en Tiendanube",
            )
        carrier = {
            "name": CARRIER_NAME,
            "callback_url": callback_url,
            "types": "ship",
            "active": True,
        }
        res = await client.post(url, json=carrier, headers=headers)
        print(f"REGISTRO CARRIER: {res.status_code} - {res.text}")
        if res.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"No se pudo registrar el carrier: {res.text}")
        carrier_id = res.json()["id"]

    # Crear las opciones de envío faltantes para los couriers activos.
    opt_url = f"{url}/{carrier_id}/options"
    opt_get = await client.get(opt_url, headers=headers)
    print(f"LISTA OPCIONES: {opt_get.status_code} - {opt_get.text[:300]}")
    if opt_get.status_code != 200:
        raise HTTPException(status_code=502, detail=f"No se pudo listar opciones: {opt_get.text}")

    codigos_existentes = {o.get("code") for o in (opt_get.json() or [])}
    activos = obtener_couriers_activos(couriers_activos)
    resultados_opciones = []
    for srv in servicios_activos(activos):
        code = srv["code"]
        nombre = srv["nombre"]
        if code in codigos_existentes:
            resultados_opciones.append(
                {"courier": srv["courier"], "code": code, "name": nombre, "status": "existente"}
            )
            continue
        opt_res = await client.post(opt_url, json={"code": code, "name": nombre}, headers=headers)
        print(f"REGISTRO OPCION {code}: {opt_res.status_code} - {opt_res.text}")
        if opt_res.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"No se pudo crear la opción {code}: {opt_res.text}")
        resultados_opciones.append(
            {
                "courier": srv["courier"],
                "code": code,
                "name": nombre,
                "status": "creada",
                "http_status": opt_res.status_code,
            }
        )

    return {
        "carrier_id": carrier_id,
        "couriers_activos": activos,
        "opciones": resultados_opciones,
    }


@app.get("/")
def read_root():
    return {
        "status": "online",
        "app": "Aromas Envios API funcionando correctamente",
        "couriers_activos": obtener_couriers_activos(),
    }


@app.get("/auth/callback")
async def auth_callback(code: str):
    if not CLIENT_ID or not CLIENT_SECRET or not PUBLIC_URL:
        raise HTTPException(
            status_code=500,
            detail="Faltan variables: TN_CLIENT_ID, TN_CLIENT_SECRET o PUBLIC_URL (revisa el archivo .env)",
        )

    async with httpx.AsyncClient(timeout=15) as client:
        # 1) Intercambiar el code (de un solo uso) por el access_token
        response = await client.post(
            TOKEN_URL,
            json={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
            },
        )
        if response.status_code != 200:
            raise HTTPException(status_code=400, detail=f"Error al autenticar: {response.text}")

        data = response.json()
        access_token = data.get("access_token")
        store_id = data.get("user_id")
        if not access_token or not store_id:
            raise HTTPException(status_code=400, detail=f"Respuesta de token incompleta: {data}")

        # 2) Registrar/actualizar el carrier y opciones de los couriers activos
        sync_res = await _sincronizar_carrier_y_opciones(
            client, str(store_id), access_token, actualizar_carrier=True
        )

        # 3) Suscribir webhooks de órdenes
        wh_res = await _suscribir_webhooks_tn(client, str(store_id), access_token)

    return {
        "mensaje": "Aplicación instalada, carrier y webhooks registrados",
        "store_id": store_id,
        "access_token": access_token,
        "carrier_id": sync_res["carrier_id"],
        "opciones": sync_res["opciones"],
        "webhooks": wh_res,
    }


@app.post("/carrier/sync-options")
async def sync_carrier_options(request: Request):
    """Endpoint administrativo protegido con X-Sync-Key para registrar en Tiendanube
    las opciones de envío faltantes de COURIERS_ACTIVOS sin pasar de nuevo por /auth/callback."""
    _validar_sync_key(request)
    body = await request.json()
    store_id = str(body.get("store_id") or "").strip()
    access_token = str(body.get("access_token") or "").strip()
    couriers_req = body.get("couriers")
    if not store_id or not access_token:
        raise HTTPException(status_code=400, detail="Falta store_id o access_token")

    async with httpx.AsyncClient(timeout=15) as client:
        sync_res = await _sincronizar_carrier_y_opciones(
            client,
            store_id,
            access_token,
            couriers_activos=couriers_req,
            actualizar_carrier=False,
        )

    return {
        "ok": True,
        "store_id": store_id,
        **sync_res,
    }


# Alias de compatibilidad hacia atrás
SERVICIOS = [
    (s["servicio"], s["code"], s["nombre"], s["tarifa_defecto"], s["phone_required"])
    for s in CATALOGO_SERVICIOS
    if s["courier"] == "blue_express"
]


def armar_rates(precios_srv: dict, couriers_activos=None) -> list:
    """Arma las tarifas para Tiendanube devolviendo solo los servicios de COURIERS_ACTIVOS.
    precios_srv: {(courier, servicio): (tarifa_courier, precio_cliente)}
    (también acepta clave por code o clave legacy de servicio para blue_express)."""
    ahora = datetime.now(TZ)
    fmt = "%Y-%m-%dT%H:%M:%S%z"  # ej: 2026-10-01T14:48:45-0300
    rates = []
    for srv in servicios_activos(couriers_activos):
        courier = srv["courier"]
        servicio = srv["servicio"]
        code = srv["code"]
        par = precios_srv.get((courier, servicio))
        if par is None:
            par = precios_srv.get(code)
        if par is None and courier == "blue_express":
            par = precios_srv.get(servicio)
        if par is None:
            continue
        costo, precio = par
        rates.append(
            {
                "name": srv["nombre"],
                "code": code,
                "price": int(round(precio)),           # tarifa_courier + costo_embalaje (si COBRAR_EMBALAJE=1)
                "price_merchant": int(round(costo)),   # tarifa_courier pura (espejo contable ERP)
                "currency": "CLP",
                "type": "ship",
                "min_delivery_date": (ahora + timedelta(days=srv["min_dias"])).strftime(fmt),
                "max_delivery_date": (ahora + timedelta(days=srv["max_dias"])).strftime(fmt),
                "phone_required": srv["phone_required"],
                "id_required": False,
                "accepts_cod": False,
                "reference": code,
            }
        )
    return rates


@app.post("/carrier/calculate")
async def calculate_shipping(request: Request):
    activos = obtener_couriers_activos()
    servicios = servicios_activos(activos)
    # (tarifa_courier, precio_cliente) provisorios si no se puede calcular
    precios_srv = {
        (srv["courier"], srv["servicio"]): (srv["tarifa_defecto"], srv["tarifa_defecto"])
        for srv in servicios
    }
    try:
        body = await request.json()
        dest = body.get("destination") or {}
        origen = ORIGEN_REGION or (body.get("origin") or {}).get("province") or ""
        items = body.get("items") or []

        paquetes_por_courier = {}
        errores_cubicaje = []
        for courier in dict.fromkeys(srv["courier"] for srv in servicios):
            try:
                paquetes = planificar_paquetes(items, courier=courier)
                paquetes_por_courier[courier] = paquetes
                print(
                    f"PAQUETES [{courier} div={obtener_divisor_volumetrico(courier):g}]: "
                    + " | ".join(
                        f"{p['unidades']}u caja {p['caja']} real={p['peso_real']:.2f}kg "
                        f"vol_caja={p['peso_vol_caja']:.2f}kg vol_prod={p['peso_vol_prod']:.2f}kg "
                        f"-> {p['tarifario']:.2f}kg talla {p['talla']}"
                        for p in paquetes
                    )
                )
            except ValueError as e:
                errores_cubicaje.append(str(e))
                print(f"SIN ENVÍO [{courier}]: {e}")

        if not paquetes_por_courier:
            desc = errores_cubicaje[0] if errores_cubicaje else "Pedido fuera de límites de despacho"
            # Error de negocio: se responde 4xx para que Tienda Nube pueda mostrar
            # el envío alternativo (contingencia) que el comerciante tenga configurado.
            return JSONResponse(
                status_code=422,
                content={"code": 422, "message": "Unprocessable Entity", "description": desc},
            )

        # En la ficha de producto y el carrito no llega la región (solo el código postal);
        # en el checkout sí llega "province".
        precios_calculados = {}
        for srv in servicios:
            courier = srv["courier"]
            servicio = srv["servicio"]
            paquetes = paquetes_por_courier.get(courier)
            if not paquetes:
                continue
            embalaje = sum(p["costo_embalaje"] for p in paquetes)
            extra = embalaje if COBRAR_EMBALAJE else 0
            region, precios = buscar_precios(dest.get("province"), origen, servicio, courier=courier)
            if precios:
                tarifa = sum(precios[p["talla"]] for p in paquetes)
            else:
                tarifa = srv["tarifa_defecto"]
            precios_calculados[(courier, servicio)] = (tarifa, tarifa + extra)
            print(
                f"TARIFA {courier}/{servicio}: destino={dest.get('province')!r} -> {region!r}, "
                f"bultos={len(paquetes)}, tarifa={tarifa:g}, embalaje={extra:g}, "
                f"cliente={tarifa + extra:g}"
            )
        precios_srv = precios_calculados
    except Exception as e:
        print(f"Error calculando envío, se usan tarifas por defecto: {e!r}")

    return {"rates": armar_rates(precios_srv, couriers_activos=activos)}


# ------------------------------------------------------------------- Webhooks
def firma_valida(raw: bytes, firma: str) -> bool:
    calc = hmac.new(CLIENT_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(calc, firma or "")


async def _webhook(request: Request):
    raw = await request.body()
    if not firma_valida(raw, request.headers.get("x-linkedstore-hmac-sha256")):
        print("Webhook con firma inválida")
        raise HTTPException(status_code=401, detail="firma inválida")
    return {"status": "ok"}


@app.post("/webhooks/store-redact")
async def store_redact(request: Request):
    return await _webhook(request)


@app.post("/webhooks/customers-redact")
async def customers_redact(request: Request):
    return await _webhook(request)


@app.post("/webhooks/customers-data-request")
async def customers_data_request(request: Request):
    return await _webhook(request)


# ------------------------------------------------- Cola Webhooks Órdenes -> ERP
import json
import sqlite3

WH_DB_PATH = os.getenv("WH_DB_PATH", "/tmp/adp_webhooks_queue.db")
SYNC_API_KEY = os.getenv("SYNC_API_KEY", "").strip() or hashlib.sha256((CLIENT_SECRET + ":adp-sync").encode()).hexdigest()[:32]

def _wh_db():
    conn = sqlite3.connect(WH_DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS webhook_events_queue (
            id_evento TEXT PRIMARY KEY,
            evento TEXT NOT NULL,
            store_id TEXT DEFAULT '',
            orden_id_externa TEXT DEFAULT '',
            fecha_recepcion_iso TEXT NOT NULL,
            hmac_valido INTEGER NOT NULL DEFAULT 1,
            payload_json TEXT NOT NULL,
            sincronizado INTEGER NOT NULL DEFAULT 0,
            sincronizado_at TEXT DEFAULT ''
        )
    """)
    conn.commit()
    return conn

def _validar_sync_key(request: Request):
    key = (request.headers.get("x-sync-key") or "").strip()
    if not key or not hmac.compare_digest(key, SYNC_API_KEY):
        raise HTTPException(status_code=401, detail="X-Sync-Key inválida")

async def _suscribir_webhooks_tn(client: httpx.AsyncClient, store_id: str, access_token: str):
    if not PUBLIC_URL or not store_id or not access_token:
        return []
    headers = {
        "Authentication": f"bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": "Aromas App (contacto@aromasdelpacifico.cl)",
    }
    wh_url = f"{API_BASE}/{store_id}/webhooks"
    target = f"{PUBLIC_URL}/webhooks/tiendanube"
    eventos_deseados = ["order/created", "order/paid", "order/fulfilled", "order/cancelled"]
    existentes = []
    r_get = await client.get(wh_url, headers=headers)
    if r_get.status_code == 200:
        existentes = r_get.json()
    registrados = []
    for ev in eventos_deseados:
        ya = next((w for w in existentes if w.get("event") == ev and w.get("url") == target), None)
        if ya:
            registrados.append({"event": ev, "id": ya.get("id"), "status": "existente"})
            continue
        r_post = await client.post(wh_url, json={"event": ev, "url": target}, headers=headers)
        registrados.append({"event": ev, "status": r_post.status_code, "resp": r_post.text[:160]})
    return registrados

@app.post("/webhooks/tiendanube")
async def recibir_webhook_tiendanube(request: Request):
    raw = await request.body()
    firma = request.headers.get("x-linkedstore-hmac-sha256") or ""
    if CLIENT_SECRET and not firma_valida(raw, firma):
        raise HTTPException(status_code=401, detail="Firma HMAC inválida")
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="JSON inválido")

    evento = str(data.get("event") or request.headers.get("x-linkedstore-topic") or "order/unknown")
    store_id = str(data.get("store_id") or "")
    orden_id = str(data.get("id") or data.get("order_id") or "")
    ahora_iso = datetime.now(TZ).isoformat()
    id_evento = hashlib.sha256(f"{store_id}:{evento}:{orden_id}:{raw.decode('utf-8', 'ignore')}".encode()).hexdigest()[:28]

    conn = _wh_db()
    conn.execute("""
        INSERT OR IGNORE INTO webhook_events_queue
        (id_evento, evento, store_id, orden_id_externa, fecha_recepcion_iso, hmac_valido, payload_json, sincronizado)
        VALUES (?, ?, ?, ?, ?, 1, ?, 0)
    """, (id_evento, evento, store_id, orden_id, ahora_iso, json.dumps(data, ensure_ascii=False)))
    conn.commit()
    conn.close()
    return {"ok": True, "id_evento": id_evento, "evento": evento, "orden_id": orden_id}

@app.get("/webhooks/pendientes")
def listar_webhooks_pendientes(request: Request, limit: int = 100):
    _validar_sync_key(request)
    conn = _wh_db()
    rows = conn.execute("""
        SELECT id_evento, evento, store_id, orden_id_externa, fecha_recepcion_iso, hmac_valido, payload_json
        FROM webhook_events_queue
        WHERE sincronizado = 0
        ORDER BY fecha_recepcion_iso ASC
        LIMIT ?
    """, (max(1, min(limit, 500)),)).fetchall()
    conn.close()
    return {"ok": True, "total": len(rows), "eventos": [dict(r) for r in rows]}

@app.post("/webhooks/ack")
async def confirmar_webhooks_sincronizados(request: Request):
    _validar_sync_key(request)
    body = await request.json()
    ids = [str(x) for x in (body.get("ids") or []) if x]
    if not ids:
        return {"ok": True, "confirmados": 0}
    conn = _wh_db()
    ahora_iso = datetime.now(TZ).isoformat()
    qmarks = ",".join("?" for _ in ids)
    conn.execute(f"UPDATE webhook_events_queue SET sincronizado = 1, sincronizado_at = ? WHERE id_evento IN ({qmarks})", [ahora_iso, *ids])
    conn.commit()
    conn.close()
    return {"ok": True, "confirmados": len(ids)}

@app.post("/webhooks/registrar")
async def endpoint_registrar_webhooks(request: Request):
    _validar_sync_key(request)
    body = await request.json()
    store_id = str(body.get("store_id") or "").strip()
    access_token = str(body.get("access_token") or "").strip()
    if not store_id or not access_token:
        raise HTTPException(status_code=400, detail="Falta store_id o access_token")
    async with httpx.AsyncClient(timeout=15) as client:
        res = await _suscribir_webhooks_tn(client, store_id, access_token)
    return {"ok": True, "webhook_url": f"{PUBLIC_URL}/webhooks/tiendanube", "resultados": res}
