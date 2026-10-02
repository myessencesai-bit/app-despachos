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

app = FastAPI(title="Aromas Envios - Carrier API", version="2.0")

# --- Configuración por variables de entorno (NO dejar secretos en el código) ---
CLIENT_ID = os.getenv("TN_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("TN_CLIENT_SECRET", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")  # https://xxxx.ngrok-free.dev
# Región desde donde despachas (para la fila "Misma Región" del CSV).
# Si la dejas vacía se usa la región de origen configurada en Tienda Nube.
ORIGEN_REGION = os.getenv("ORIGEN_REGION", "").strip()
# Divisor del peso volumétrico de Blue Express (largo x ancho x alto en cm / 4000)
DIVISOR_VOLUMETRICO = float(os.getenv("DIVISOR_VOLUMETRICO", "4000"))
# 1 = el costo de embalaje (caja, burbuja, cinta...) se suma al precio que paga el cliente
COBRAR_EMBALAJE = os.getenv("COBRAR_EMBALAJE", "1").strip() == "1"
# Máximo de bultos por pedido; sobre eso se responde 422 (pedido a coordinar aparte)
MAX_PAQUETES = int(os.getenv("MAX_PAQUETES", "10"))
print(f"DEBUG credenciales: client_id={CLIENT_ID!r}, largo_secret={len(CLIENT_SECRET)}, public_url={PUBLIC_URL!r}")

API_BASE = "https://api.tiendanube.com/v1"
TOKEN_URL = "https://www.tiendanube.com/apps/authorize/token"
CARRIER_NAME = "Blue Express"
OPTION_CODE = "blue_express_standard"  # domicilio (debe coincidir EXACTO con el "code" de las tarifas)
PUNTO_CODE = "blue_express_punto"      # retiro en punto Blue Express
OPCIONES = [
    (OPTION_CODE, "Blue Express - Envío a domicilio"),
    (PUNTO_CODE, "Blue Express - Retiro en punto"),
]
# Tarifas provisorias cuando no se puede determinar la región (p. ej. en el carrito)
TARIFA_DEFECTO = 4500
TARIFA_DEFECTO_PUNTO = int(os.getenv("TARIFA_DEFECTO_PUNTO", str(TARIFA_DEFECTO)))
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


def cargar_tarifas() -> dict:
    """tarifas.csv -> {(region, tipo_servicio): {"xs": precio, "s": precio, ...}}"""
    global _tarifas
    if _tarifas is not None:
        return _tarifas
    _tarifas = {}
    filas = leer_csv("tarifas.csv")
    if filas is None:
        print("AVISO: no existe tarifas.csv, se usarán las tarifas por defecto")
        return _tarifas
    for row in filas:
        try:
            servicio = row["tipo_servicio"].strip().lower()
            precios = {t: _num(row[t]) for t, _ in TALLAS}
            _tarifas[(clave(row["region"]), servicio)] = precios
        except Exception as e:
            print(f"Fila inválida en tarifas.csv {row}: {e!r}")
    print(f"Tarifas cargadas: {len(_tarifas)} filas (región + servicio)")
    return _tarifas


def talla_para(kg: float):
    """Talla según el peso tarifario, o None si excede el máximo (25 kg)."""
    for nombre, maximo in TALLAS:
        if kg <= maximo:
            return nombre
    return None


def buscar_precios(provincia: str, origen: str, servicio: str = "domicilio"):
    """Devuelve (región encontrada, precios por talla) o (None, None)."""
    tarifas = cargar_tarifas()
    k = clave(provincia)
    if not k:
        return None, None
    if origen and k == clave(origen) and ("misma", servicio) in tarifas:
        return "misma región", tarifas[("misma", servicio)]
    if (k, servicio) in tarifas:
        return k, tarifas[(k, servicio)]
    for (reg, serv), precios in tarifas.items():  # coincidencia parcial (p. ej. "O'Higgins")
        if serv == servicio and reg != "misma" and (k in reg or reg in k):
            return reg, precios
    return None, None


# ------------------------------------------------------------ Cajas (Opción C)
_cajas = None


def cargar_cajas() -> list:
    """cajas.csv -> lista ordenada por capacidad:
    max_unidades, largo_cm, ancho_cm, alto_cm, peso_embalaje_kg, costo_embalaje, nota"""
    global _cajas
    if _cajas is not None:
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


def planificar_paquetes(items: list) -> list:
    """Reparte las unidades del carrito en cajas y calcula el peso tarifario de cada bulto:
        peso tarifario = el mayor entre
            - peso real (productos + embalaje)
            - peso volumétrico de la caja (largo x ancho x alto / 4000)
            - peso volumétrico de los productos (por si no caben en la caja elegida)
    Lanza ValueError si el pedido no se puede despachar."""
    unidades = sum(int(i.get("quantity") or 1) for i in items) or 1
    gramos = sum(float(i.get("grams") or 0) * int(i.get("quantity") or 1) for i in items)
    vol_cm3 = 0.0
    for i in items:
        d = i.get("dimensions") or {}
        try:
            vol_cm3 += (
                float(d.get("width") or 0) * float(d.get("height") or 0) * float(d.get("depth") or 0)
                * int(i.get("quantity") or 1)
            )
        except (TypeError, ValueError):
            pass
    if gramos <= 0:
        print("AVISO: hay productos sin peso cargado en Tienda Nube (grams = 0)")
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
        peso_real = u * g_unidad / 1000 + (caja["peso_embalaje"] if caja else 0)
        vol_caja = (caja["largo"] * caja["ancho"] * caja["alto"] / DIVISOR_VOLUMETRICO) if caja else 0
        vol_prod = u * vol_unidad / DIVISOR_VOLUMETRICO
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
                "talla": talla,
                "costo_embalaje": caja["costo_embalaje"] if caja else 0,
            }
        )
    return paquetes


# ---------------------------------------------------------------------- Rutas
@app.get("/")
def read_root():
    return {"status": "online", "app": "Aromas Envios API funcionando correctamente"}


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

        # TODO: guardar (store_id, access_token) en una base de datos

        # 2) Registrar o actualizar el carrier
        headers = {
            "Authentication": f"bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": "Aromas App (contacto@aromasdelpacifico.cl)",  # solo ASCII
        }
        url = f"{API_BASE}/{store_id}/shipping_carriers"
        carrier = {
            "name": CARRIER_NAME,
            "callback_url": f"{PUBLIC_URL}/carrier/calculate",  # debe ser HTTPS
            "types": "ship",
            "active": True,
        }

        get_res = await client.get(url, headers=headers)
        print(f"LISTA CARRIERS: {get_res.status_code} - {get_res.text[:300]}")
        if get_res.status_code != 200:
            raise HTTPException(status_code=502, detail=f"No se pudo listar carriers: {get_res.text}")

        existente = next((c for c in get_res.json() if c.get("name") == CARRIER_NAME), None)
        if existente:
            res = await client.put(f"{url}/{existente['id']}", json=carrier, headers=headers)
        else:
            res = await client.post(url, json=carrier, headers=headers)

        print(f"REGISTRO CARRIER: {res.status_code} - {res.text}")
        if res.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"No se pudo registrar el carrier: {res.text}")

        carrier_id = existente["id"] if existente else res.json()["id"]

        # 3) Crear las opciones de envío. Sin ellas Tienda Nube no muestra tarifas:
        #    el "code" de cada tarifa debe corresponder a una opción existente.
        opt_url = f"{url}/{carrier_id}/options"
        opt_get = await client.get(opt_url, headers=headers)
        print(f"LISTA OPCIONES: {opt_get.status_code} - {opt_get.text[:300]}")
        if opt_get.status_code != 200:
            raise HTTPException(status_code=502, detail=f"No se pudo listar opciones: {opt_get.text}")

        codigos_existentes = {o.get("code") for o in opt_get.json()}
        for code, nombre in OPCIONES:
            if code in codigos_existentes:
                continue
            opt_res = await client.post(opt_url, json={"code": code, "name": nombre}, headers=headers)
            print(f"REGISTRO OPCION {code}: {opt_res.status_code} - {opt_res.text}")
            if opt_res.status_code >= 400:
                raise HTTPException(status_code=502, detail=f"No se pudo crear la opción {code}: {opt_res.text}")

    return {"mensaje": "Aplicación instalada, carrier y opciones de envío registrados", "store_id": store_id}


# (servicio en el CSV, code de la opción, nombre visible, tarifa por defecto, exige teléfono)
SERVICIOS = [
    ("domicilio", OPTION_CODE, "Blue Express - Envío a domicilio", TARIFA_DEFECTO, False),
    ("punto", PUNTO_CODE, "Blue Express - Retiro en punto", TARIFA_DEFECTO_PUNTO, True),
]


def armar_rates(precios_srv: dict) -> list:
    """precios_srv: {servicio: (costo Blue Express, precio al cliente)}"""
    ahora = datetime.now(TZ)
    fmt = "%Y-%m-%dT%H:%M:%S%z"  # ej: 2026-10-01T14:48:45-0300
    rates = []
    for servicio, code, nombre, _defecto, telefono in SERVICIOS:
        costo, precio = precios_srv[servicio]
        rates.append(
            {
                "name": nombre,
                "code": code,
                "price": int(round(precio)),           # lo que paga el cliente
                "price_merchant": int(round(costo)),   # lo que cuesta al comerciante (Blue Express)
                "currency": "CLP",
                "type": "ship",
                "min_delivery_date": (ahora + timedelta(days=2)).strftime(fmt),
                "max_delivery_date": (ahora + timedelta(days=3)).strftime(fmt),
                "phone_required": telefono,
                "id_required": False,
                "accepts_cod": False,
                "reference": code,
            }
        )
    return rates


@app.post("/carrier/calculate")
async def calculate_shipping(request: Request):
    # (costo Blue Express, precio al cliente) provisorios si no se puede calcular
    precios_srv = {s: (d, d) for s, _c, _n, d, _t in SERVICIOS}
    try:
        body = await request.json()
        dest = body.get("destination") or {}
        origen = ORIGEN_REGION or (body.get("origin") or {}).get("province") or ""
        items = body.get("items") or []

        try:
            paquetes = planificar_paquetes(items)
        except ValueError as e:
            print(f"SIN ENVÍO: {e}")
            # Error de negocio: se responde 4xx para que Tienda Nube pueda mostrar
            # el envío alternativo (contingencia) que el comerciante tenga configurado.
            return JSONResponse(
                status_code=422,
                content={"code": 422, "message": "Unprocessable Entity", "description": str(e)},
            )

        print(
            "PAQUETES: "
            + " | ".join(
                f"{p['unidades']}u caja {p['caja']} real={p['peso_real']:.2f}kg "
                f"vol_caja={p['peso_vol_caja']:.2f}kg vol_prod={p['peso_vol_prod']:.2f}kg "
                f"-> {p['tarifario']:.2f}kg talla {p['talla']}"
                for p in paquetes
            )
        )

        embalaje = sum(p["costo_embalaje"] for p in paquetes)
        extra = embalaje if COBRAR_EMBALAJE else 0

        # En la ficha de producto y el carrito no llega la región (solo el código postal);
        # en el checkout sí llega "province".
        for servicio, *_ in SERVICIOS:
            region, precios = buscar_precios(dest.get("province"), origen, servicio)
            if precios:
                tarifa = sum(precios[p["talla"]] for p in paquetes)
            else:
                tarifa = precios_srv[servicio][0]
            precios_srv[servicio] = (tarifa, tarifa + extra)
            print(
                f"TARIFA {servicio}: destino={dest.get('province')!r} -> {region!r}, "
                f"bultos={len(paquetes)}, tarifa={tarifa:g}, embalaje={extra:g}, "
                f"cliente={tarifa + extra:g}"
            )
    except Exception as e:
        print(f"Error calculando envío, se usan tarifas por defecto: {e!r}")

    return {"rates": armar_rates(precios_srv)}


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