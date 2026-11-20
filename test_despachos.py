"""
Suite de pruebas automatizadas — App_Despachos (Etapa 2 Logística, Bodega y Cubicaje).
Ejecutar con: python test_despachos.py
"""
import hashlib
import hmac
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from unittest.mock import patch

from fastapi.testclient import TestClient

# Asegurar importación local desde App_Despachos/
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import main  # noqa: E402

client = TestClient(main.app)


@contextmanager
def env_couriers(valor: str):
    anterior = os.environ.get("COURIERS_ACTIVOS")
    os.environ["COURIERS_ACTIVOS"] = valor
    try:
        yield
    finally:
        if anterior is None:
            os.environ.pop("COURIERS_ACTIVOS", None)
        else:
            os.environ["COURIERS_ACTIVOS"] = anterior


def _rates_por_codigo(resp_json: dict) -> dict:
    return {r["code"]: r for r in resp_json.get("rates", [])}


def test_1_orden_estandar_1_perfume_metropolitana():
    """Test 1: 1 perfume a Metropolitana (Blue Express) con caja chica +$750."""
    main.cargar_cajas(force_reload=True)
    main.cargar_tarifas(force_reload=True)

    items = [
        {
            "quantity": 1,
            "grams": 450,
            "dimensions": {"width": 15, "height": 10, "depth": 8},
        }
    ]
    paquetes = main.planificar_paquetes(items, courier="blue_express")
    assert len(paquetes) == 1
    p = paquetes[0]
    assert p["caja"] == "22x16x12", f"Caja inesperada: {p['caja']}"
    assert p["costo_embalaje"] == 750, f"Embalaje esperado 750, obtenido {p['costo_embalaje']}"
    assert abs(p["peso_real"] - 0.65) < 1e-6, f"Peso real inesperado: {p['peso_real']}"
    assert abs(p["peso_vol_caja"] - 1.056) < 1e-6, f"Vol caja inesperado: {p['peso_vol_caja']}"
    assert abs(p["peso_vol_prod"] - 0.30) < 1e-6, f"Vol prod inesperado: {p['peso_vol_prod']}"
    assert abs(p["tarifario"] - 1.056) < 1e-6
    assert p["talla"] == "s"

    with env_couriers("blue_express"):
        r = client.post(
            "/carrier/calculate",
            json={
                "origin": {"province": "Valparaíso"},
                "destination": {"province": "Metropolitana de Santiago"},
                "items": items,
            },
        )
    assert r.status_code == 200, r.text
    rates = _rates_por_codigo(r.json())
    assert set(rates.keys()) == {"blue_express_standard", "blue_express_punto"}

    punto = rates["blue_express_punto"]
    domicilio = rates["blue_express_standard"]
    assert punto["name"] == "Blue Express - Retiro en punto"
    assert domicilio["name"] == "Blue Express - Envío a domicilio"
    assert punto["price_merchant"] == 2850, f"Punto merchant esperado 2850, obtenido {punto['price_merchant']}"
    assert punto["price"] == 3600, f"Punto cliente esperado 3600, obtenido {punto['price']}"
    assert domicilio["price_merchant"] == 3650, f"Domicilio merchant esperado 3650, obtenido {domicilio['price_merchant']}"
    assert domicilio["price"] == 4400, f"Domicilio cliente esperado 4400, obtenido {domicilio['price']}"


def test_2_orden_multi_frasco_3_perfumes_valparaiso():
    """Test 2: 3 perfumes a Valparaíso -> caja 28x22x18 (+$1.200) y peso físico vs volumétrico."""
    # Caso 2A: 3 perfumes estándar (450g c/u) -> domina peso volumétrico de caja (2.772 kg > 1.70 kg) -> talla s
    items_vol = [
        {
            "quantity": 3,
            "grams": 450,
            "dimensions": {"width": 15, "height": 10, "depth": 8},
        }
    ]
    paq_vol = main.planificar_paquetes(items_vol, courier="blue_express")
    assert len(paq_vol) == 1
    pv = paq_vol[0]
    assert pv["caja"] == "28x22x18", f"Caja esperada 28x22x18, obtenida {pv['caja']}"
    assert pv["costo_embalaje"] == 1200, f"Embalaje esperado 1200, obtenido {pv['costo_embalaje']}"
    assert abs(pv["peso_real"] - 1.70) < 1e-6       # 3 * 0.45 + 0.35
    assert abs(pv["peso_vol_caja"] - 2.772) < 1e-6  # 28 * 22 * 18 / 4000
    assert abs(pv["peso_vol_prod"] - 0.90) < 1e-6   # 3 * 1200 / 4000
    assert abs(pv["tarifario"] - 2.772) < 1e-6
    assert pv["talla"] == "s"

    with env_couriers("blue_express"):
        r_vol = client.post(
            "/carrier/calculate",
            json={
                "origin": {"province": "Metropolitana de Santiago"},
                "destination": {"province": "Valparaíso"},
                "items": items_vol,
            },
        )
    assert r_vol.status_code == 200
    rates_vol = _rates_por_codigo(r_vol.json())
    assert rates_vol["blue_express_punto"]["price_merchant"] == 3700
    assert rates_vol["blue_express_punto"]["price"] == 4900      # 3700 + 1200
    assert rates_vol["blue_express_standard"]["price_merchant"] == 4500
    assert rates_vol["blue_express_standard"]["price"] == 5700   # 4500 + 1200

    # Caso 2B: 3 frascos pesados (1100g c/u) -> domina peso físico real (3.65 kg > 2.772 kg) -> talla m
    items_fis = [
        {
            "quantity": 3,
            "grams": 1100,
            "dimensions": {"width": 15, "height": 10, "depth": 8},
        }
    ]
    paq_fis = main.planificar_paquetes(items_fis, courier="blue_express")
    pf = paq_fis[0]
    assert pf["caja"] == "28x22x18"
    assert pf["costo_embalaje"] == 1200
    assert abs(pf["peso_real"] - 3.65) < 1e-6       # 3 * 1.10 + 0.35
    assert abs(pf["peso_vol_caja"] - 2.772) < 1e-6
    assert abs(pf["tarifario"] - 3.65) < 1e-6
    assert pf["talla"] == "m"

    with env_couriers("blue_express"):
        r_fis = client.post(
            "/carrier/calculate",
            json={
                "origin": {"province": "Metropolitana de Santiago"},
                "destination": {"province": "Valparaíso"},
                "items": items_fis,
            },
        )
    assert r_fis.status_code == 200
    rates_fis = _rates_por_codigo(r_fis.json())
    assert rates_fis["blue_express_punto"]["price_merchant"] == 5200
    assert rates_fis["blue_express_punto"]["price"] == 6400      # 5200 + 1200
    assert rates_fis["blue_express_standard"]["price_merchant"] == 6000
    assert rates_fis["blue_express_standard"]["price"] == 7200   # 6000 + 1200


def test_3_fallback_grams_0_y_sin_dimensiones():
    """Test 3: Producto con grams=0 y sin dimensiones aplica fallback 0.45 kg y 15x10x8 cm (no cae a xs)."""
    for items_caso in [
        [{"quantity": 1, "grams": 0}],
        [{"quantity": 1, "grams": 0, "dimensions": {"width": 0, "height": 0, "depth": 0}}],
        [{"quantity": 1, "grams": None, "dimensions": None}],
    ]:
        paquetes = main.planificar_paquetes(items_caso, courier="blue_express")
        assert len(paquetes) == 1
        p = paquetes[0]
        assert p["fallback_peso_aplicado"] is True
        assert p["fallback_dim_aplicado"] is True
        assert abs(p["peso_real"] - 0.65) < 1e-6        # 0.45 fallback + 0.20 caja chica
        assert abs(p["peso_vol_prod"] - 0.30) < 1e-6    # (15*10*8=1200) / 4000
        assert abs(p["peso_vol_caja"] - 1.056) < 1e-6   # (22*16*12=4224) / 4000
        assert abs(p["tarifario"] - 1.056) < 1e-6
        assert p["talla"] == "s", f"No debe caer a xs, talla obtenida: {p['talla']}"

    # Verificar también vía endpoint HTTP que cotiza en talla 's' (2850/3650) y NO 'xs' (2300/3100)
    with env_couriers("blue_express"):
        r = client.post(
            "/carrier/calculate",
            json={
                "origin": {"province": "Valparaíso"},
                "destination": {"province": "Metropolitana de Santiago"},
                "items": [{"quantity": 1, "grams": 0}],
            },
        )
    assert r.status_code == 200
    rates = _rates_por_codigo(r.json())
    assert rates["blue_express_punto"]["price_merchant"] == 2850
    assert rates["blue_express_punto"]["price"] == 3600
    assert rates["blue_express_standard"]["price_merchant"] == 3650
    assert rates["blue_express_standard"]["price"] == 4400


def test_4_filtro_couriers_activos():
    """Test 4: COURIERS_ACTIVOS='blue_express' devuelve solo 2 opciones; 'blue_express,starken' devuelve ambas familias."""
    payload = {
        "origin": {"province": "Valparaíso"},
        "destination": {"province": "Metropolitana de Santiago"},
        "items": [{"quantity": 1, "grams": 450, "dimensions": {"width": 15, "height": 10, "depth": 8}}],
    }

    # 4A: Solo Blue Express
    with env_couriers("blue_express"):
        r1 = client.post("/carrier/calculate", json=payload)
    assert r1.status_code == 200
    rates1 = r1.json()["rates"]
    assert len(rates1) == 2, f"Se esperaban 2 opciones, llegaron {len(rates1)}"
    assert {x["code"] for x in rates1} == {"blue_express_standard", "blue_express_punto"}

    # 4B: Blue Express + Starken
    with env_couriers("blue_express,starken"):
        r2 = client.post("/carrier/calculate", json=payload)
    assert r2.status_code == 200
    rates2 = _rates_por_codigo(r2.json())
    assert len(rates2) == 4, f"Se esperaban 4 opciones, llegaron {len(rates2)}"
    assert set(rates2.keys()) == {
        "blue_express_standard",
        "blue_express_punto",
        "starken_domicilio",
        "starken_agencia",
    }
    assert rates2["blue_express_standard"]["name"] == "Blue Express - Envío a domicilio"
    assert rates2["blue_express_punto"]["name"] == "Blue Express - Retiro en punto"
    assert rates2["starken_domicilio"]["name"] == "Starken - Envío a domicilio"
    assert rates2["starken_agencia"]["name"] == "Starken - Retiro en agencia"

    # Validar espejo contable ERP (price_merchant = tarifa pura, price = tarifa + embalaje 750)
    assert rates2["starken_domicilio"]["price_merchant"] == 4000
    assert rates2["starken_domicilio"]["price"] == 4750
    assert rates2["starken_agencia"]["price_merchant"] == 3200
    assert rates2["starken_agencia"]["price"] == 3950


def test_5_divisores_volumetricos_por_courier():
    """Test 5: Divisores volumétricos 4000 (blue_express, starken) y 5000 (chilexpress)."""
    assert main.obtener_divisor_volumetrico("blue_express") == 4000.0
    assert main.obtener_divisor_volumetrico("starken") == 4000.0
    assert main.obtener_divisor_volumetrico("chilexpress") == 5000.0

    items = [{"quantity": 1, "grams": 450, "dimensions": {"width": 15, "height": 10, "depth": 8}}]
    p_blue = main.planificar_paquetes(items, courier="blue_express")[0]
    p_chx = main.planificar_paquetes(items, courier="chilexpress")[0]
    assert abs(p_blue["peso_vol_caja"] - (4224 / 4000)) < 1e-6
    assert abs(p_chx["peso_vol_caja"] - (4224 / 5000)) < 1e-6
    assert p_chx["divisor_volumetrico"] == 5000.0


def test_6_compatibilidad_tarifas_sin_columna_courier():
    """Test 6: Compatibilidad hacia atrás si una fila de tarifas.csv omite la columna 'courier'."""
    filas_legacy = [
        {
            "region": "Metropolitana de Santiago",
            "tipo_servicio": "domicilio",
            "xs": "3100",
            "s": "3650",
            "m": "4700",
            "l": "5700",
            "xl": "7600",
        }
    ]
    with patch.object(main, "leer_csv", return_value=filas_legacy):
        tarifas = main.cargar_tarifas(force_reload=True)
        assert ("blue_express", "metropolitanadesantiago", "domicilio") in tarifas
        assert tarifas[("blue_express", "metropolitanadesantiago", "domicilio")]["s"] == 3650.0
    # Restaurar tarifas reales del disco
    main.cargar_tarifas(force_reload=True)


def test_7_endpoint_sync_options():
    """Test 7: POST /carrier/sync-options protegido con X-Sync-Key y crea solo opciones faltantes."""
    # Sin X-Sync-Key -> 401
    r_unauth = client.post("/carrier/sync-options", json={"store_id": "123", "access_token": "tok"})
    assert r_unauth.status_code == 401

    # Sin store_id o access_token -> 400
    r_bad = client.post(
        "/carrier/sync-options",
        headers={"X-Sync-Key": main.SYNC_API_KEY},
        json={"store_id": "123"},
    )
    assert r_bad.status_code == 400

    # Mock de httpx.AsyncClient simulando carrier existente con solo Blue Express registrado
    llamadas_post = []

    class FakeResp:
        def __init__(self, status_code, data):
            self.status_code = status_code
            self._data = data
            self.text = json.dumps(data)

        def json(self):
            return self._data

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers=None):
            if url.endswith("/shipping_carriers"):
                return FakeResp(200, [{"id": 999, "name": main.CARRIER_NAME}])
            if url.endswith("/shipping_carriers/999/options"):
                return FakeResp(
                    200,
                    [
                        {"code": "blue_express_standard", "name": "Blue Express - Envío a domicilio"},
                        {"code": "blue_express_punto", "name": "Blue Express - Retiro en punto"},
                    ],
                )
            return FakeResp(404, {})

        async def post(self, url, json=None, headers=None):
            llamadas_post.append((url, json))
            return FakeResp(201, {"id": 1000 + len(llamadas_post), **(json or {})})

    with env_couriers("blue_express,starken"), patch.object(main.httpx, "AsyncClient", FakeAsyncClient):
        r_ok = client.post(
            "/carrier/sync-options",
            headers={"X-Sync-Key": main.SYNC_API_KEY},
            json={"store_id": "55555", "access_token": "fake_token_abc"},
        )
    assert r_ok.status_code == 200, r_ok.text
    body = r_ok.json()
    assert body["ok"] is True
    assert body["carrier_id"] == 999
    assert len(llamadas_post) == 2, f"Solo debía crear 2 opciones de Starken, creó: {llamadas_post}"
    creados = {p[1]["code"] for p in llamadas_post}
    assert creados == {"starken_domicilio", "starken_agencia"}
    estados = {o["code"]: o["status"] for o in body["opciones"]}
    assert estados == {
        "blue_express_standard": "existente",
        "blue_express_punto": "existente",
        "starken_domicilio": "creada",
        "starken_agencia": "creada",
    }


def test_8_compatibilidad_rutas_y_webhooks():
    """Test 8: Todas las rutas críticas preservadas y cola de webhooks funcional."""
    rutas_existentes = {r.path for r in main.app.routes}
    rutas_obligatorias = {
        "/",
        "/auth/callback",
        "/carrier/calculate",
        "/carrier/sync-options",
        "/webhooks/tiendanube",
        "/webhooks/pendientes",
        "/webhooks/ack",
        "/webhooks/registrar",
    }
    faltantes = rutas_obligatorias - rutas_existentes
    assert not faltantes, f"Faltan rutas obligatorias: {faltantes}"

    # GET /
    r_root = client.get("/")
    assert r_root.status_code == 200
    assert r_root.json()["status"] == "online"

    # Flujo completo webhooks con BD SQLite temporal
    with tempfile.TemporaryDirectory() as tmpdir:
        db_tmp = os.path.join(tmpdir, "test_wh.db")
        with patch.object(main, "WH_DB_PATH", db_tmp):
            payload = {"event": "order/paid", "store_id": 12345, "id": 98765}
            raw = json.dumps(payload).encode("utf-8")
            firma = hmac.new(main.CLIENT_SECRET.encode(), raw, hashlib.sha256).hexdigest()

            r_wh = client.post(
                "/webhooks/tiendanube",
                content=raw,
                headers={
                    "Content-Type": "application/json",
                    "x-linkedstore-hmac-sha256": firma,
                },
            )
            assert r_wh.status_code == 200, r_wh.text
            ev_id = r_wh.json()["id_evento"]

            r_pend = client.get("/webhooks/pendientes", headers={"X-Sync-Key": main.SYNC_API_KEY})
            assert r_pend.status_code == 200
            assert r_pend.json()["total"] == 1
            assert r_pend.json()["eventos"][0]["id_evento"] == ev_id

            r_ack = client.post(
                "/webhooks/ack",
                headers={"X-Sync-Key": main.SYNC_API_KEY},
                json={"ids": [ev_id]},
            )
            assert r_ack.status_code == 200
            assert r_ack.json()["confirmados"] == 1

            r_pend2 = client.get("/webhooks/pendientes", headers={"X-Sync-Key": main.SYNC_API_KEY})
            assert r_pend2.json()["total"] == 0


def main_runner():
    tests = [
        ("1. Orden estándar 1 perfume a Metropolitana (Blue Express)", test_1_orden_estandar_1_perfume_metropolitana),
        ("2. Orden multi-frasco 3 perfumes a Valparaíso (caja 28x22x18 +$1.200)", test_2_orden_multi_frasco_3_perfumes_valparaiso),
        ("3. Fallback grams=0 y sin dimensiones (0.45 kg / 15x10x8 cm)", test_3_fallback_grams_0_y_sin_dimensiones),
        ("4. Filtro COURIERS_ACTIVOS (blue_express vs blue_express,starken)", test_4_filtro_couriers_activos),
        ("5. Divisores volumétricos por courier (4000 / 5000)", test_5_divisores_volumetricos_por_courier),
        ("6. Compatibilidad tarifas.csv legacy sin columna courier", test_6_compatibilidad_tarifas_sin_columna_courier),
        ("7. Endpoint administrativo POST /carrier/sync-options", test_7_endpoint_sync_options),
        ("8. Preservación de rutas y flujo cola webhooks", test_8_compatibilidad_rutas_y_webhooks),
    ]
    print("=" * 72)
    print("SUITE DE PRUEBAS APP_DESPACHOS — ETAPA 2 (LOGÍSTICA Y CUBICAJE)")
    print("=" * 72)
    fallos = 0
    for nombre, fn in tests:
        try:
            fn()
            print(f"[PASS] {nombre}")
        except Exception as e:
            fallos += 1
            print(f"[FAIL] {nombre} -> {type(e).__name__}: {e}")
    print("-" * 72)
    total = len(tests)
    print(f"RESULTADO: {total - fallos}/{total} pruebas exitosas")
    print("=" * 72)
    if fallos:
        sys.exit(1)


if __name__ == "__main__":
    main_runner()
