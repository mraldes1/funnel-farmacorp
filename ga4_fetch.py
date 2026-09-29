"""Conexión a GA4 (Google Analytics Data API) para los funnels de Farmacorp
(la app -- una super app -- y la web, dos propiedades de GA4 separadas).

Copia adaptada de dinex_tower/ga4_fetch.py para el deploy standalone en
Streamlit Cloud (Mariano, 2026-09-29) -- la lógica de negocio (pasos del
funnel, ventana de 30 min, prorrateo FA/AM) es idéntica; lo único que cambia
es DE DÓNDE se leen las credenciales y la base de comparación:
- Credenciales de GA4: antes archivo JSON en disco, ahora `st.secrets`
  (mismo service account, no es una credencial nueva).
- `dist_negocio()`: antes `dinex.db` local (sqlite, no existe en la nube),
  ahora la tabla `resumen_kpis_diarios` de Neon/Postgres -- la misma base
  que ya usa dinex_cloud, mismo criterio de "la nube es liviana a propósito".

Property IDs confirmados con Mariano: App = 470615795, Web = 429901046.

## Por qué el funnel usa "dentro de N minutos del paso anterior" y no personas sueltas

Probado en vivo 2026-08-30: un funnel de personas SIN restricción de tiempo entre
pasos (cualquiera que haya hecho cada evento en algún momento del período, sin
importar cuándo) da una conversión de 1,77% -- pero mezcla gente que abrió la app
un día y compró semanas después como si fuera UN SOLO intento de compra. Eso no
sirve para diagnóstico de UX (Lu necesita saber dónde se rompe un intento real,
no la tasa de conversión de vida del usuario). Con la restricción de 30 minutos
entre cada paso (aprox. una sola visita, mismo criterio que usa GA4 para definir
una sesión) la conversión real sale 0,35% -- 5 veces más baja, y es la que
efectivamente sirve para ver fricción de producto.

## Por qué NO confiar en el "purchase" de acá para saber cuánto se vendió

El evento `purchase` de GA4 se usa acá SOLO para medir dónde se cae la gente en
el funnel (fricción de UX), nunca como número de venta real -- la fuente de
verdad de ventas es Oracle, igual que ya se hace en la Sheets "Fuente Datos
Nexocorp". El tracking de analytics pierde ventas reales por bloqueadores de
anuncios/tracking, fallas de red al confirmar, o gente que compra por WhatsApp/
Contact Center (fuera de la app/web).

## Prorrateo Farmacorp/Amarket dentro de la App

La App es UNA sola propiedad de GA4 compartida entre Farmacorp y Amarket -- GA4
no la distingue por sí solo (las dimensiones candidatas -- affiliation,
contentGroup, unifiedScreenName, itemBrand, itemCategory -- vienen vacías/sin
instrumentar). Mismo método que ya usa Hugo en la Sheets "Fuente Datos
Nexocorp": el % de transacciones reales (Farmacorp vs Amarket, mismo período)
se aplica por igual a CADA paso del funnel.
"""
from __future__ import annotations

from datetime import date
from functools import lru_cache

CREDS_SECRET_KEY = "ga4_service_account"

# ── Propiedades y overrides por propiedad ──────────────────────────────────
PROPIEDADES = {
    "App": {
        "id": "470615795",
        "compra_overrides": {0: ("Abrió la app", None)},
        "onboarding_overrides": {0: ("Abrió por primera vez", None)},
    },
    "Web": {
        "id": "429901046",
        "compra_overrides": {0: ("Entró al sitio", None), 7: (None, "payment_info")},
        "onboarding_overrides": {0: ("Entró por primera vez", "first_visit")},
    },
}

PASOS_COMPRA = [
    ("Abrió la app", "session_start"),
    ("Vio un listado", "view_item_list"),
    ("Vio un producto", "view_item"),
    ("Agregó al carrito", "add_to_cart"),
    ("Vio el carrito", "view_cart"),
    ("Inició el checkout", "begin_checkout"),
    ("Puso datos de envío", "add_shipping_info"),
    ("Puso forma de pago", "add_payment_info"),
    ("Compró", "purchase"),
]

PASOS_ONBOARDING = [
    ("Abrió por primera vez", "first_open"),
    ("Inició sesión", "login"),
    ("Exploró productos", "view_item_list"),
]

EVENTO_INSTALL = "first_open"
EVENTO_UNINSTALL = "app_remove"

DENTRO_DE_MIN = 30  # ventana entre paso y paso -- mismo criterio que una sesión de GA4


def creds_disponibles() -> bool:
    import streamlit as st
    return CREDS_SECRET_KEY in st.secrets


def _get_creds():
    import streamlit as st
    from google.oauth2.service_account import Credentials
    return Credentials.from_service_account_info(
        dict(st.secrets[CREDS_SECRET_KEY]),
        scopes=["https://www.googleapis.com/auth/analytics.readonly"],
    )


def _resolver_pasos(pasos_base: list[tuple[str, str]], overrides: dict) -> list[tuple[str, str]]:
    pasos = list(pasos_base)
    for i, (etiqueta_o, evento_o) in overrides.items():
        etiqueta_base, evento_base = pasos[i]
        pasos[i] = (etiqueta_o or etiqueta_base, evento_o or evento_base)
    return pasos


def fetch_funnel(fecha_desde: date, fecha_hasta: date, propiedad: str = "App",
                  tipo: str = "compra", dentro_de_min: int = DENTRO_DE_MIN) -> list[dict]:
    """Trae un funnel (personas únicas por paso, cada paso dentro de
    `dentro_de_min` minutos del anterior -- ver docstring del módulo) para el
    rango de fechas dado. `propiedad` = 'App' o 'Web'. `tipo` = 'compra' o
    'onboarding'. Devuelve una lista de dicts en orden de los pasos:
    {paso, evento, usuarios, pct_continua, abandonan, pct_abandona}."""
    from google.analytics.data_v1alpha import AlphaAnalyticsDataClient
    from google.analytics.data_v1alpha.types import (
        RunFunnelReportRequest, DateRange, Funnel, FunnelStep,
        FunnelFilterExpression, FunnelEventFilter,
    )
    from google.protobuf.duration_pb2 import Duration

    prop = PROPIEDADES[propiedad]
    if tipo == "compra":
        pasos = _resolver_pasos(PASOS_COMPRA, prop["compra_overrides"])
    elif tipo == "onboarding":
        pasos = _resolver_pasos(PASOS_ONBOARDING, prop["onboarding_overrides"])
    else:
        raise ValueError(f"tipo desconocido: {tipo!r} (usar 'compra' u 'onboarding')")

    client = AlphaAnalyticsDataClient(credentials=_get_creds())

    pasos_proto = []
    for i, (etiqueta, evento) in enumerate(pasos):
        kwargs = dict(
            name=etiqueta,
            filter_expression=FunnelFilterExpression(
                funnel_event_filter=FunnelEventFilter(event_name=evento)
            ),
        )
        if i > 0:  # el primer paso no tiene "paso anterior" del cual acotarse
            d = Duration()
            d.FromSeconds(dentro_de_min * 60)
            kwargs["within_duration_from_prior_step"] = d
        pasos_proto.append(FunnelStep(**kwargs))

    req = RunFunnelReportRequest(
        property=f"properties/{prop['id']}",
        date_ranges=[DateRange(start_date=fecha_desde.isoformat(), end_date=fecha_hasta.isoformat())],
        funnel=Funnel(is_open_funnel=False, steps=pasos_proto),
    )
    resp = client.run_funnel_report(req)

    filas = []
    for i, row in enumerate(resp.funnel_table.rows):
        usuarios = int(row.metric_values[0].value)
        pct_continua = float(row.metric_values[1].value) if len(row.metric_values) > 1 else None
        abandonan = int(row.metric_values[2].value) if len(row.metric_values) > 2 else None
        pct_abandona = float(row.metric_values[3].value) if len(row.metric_values) > 3 else None
        etiqueta, evento = pasos[i]
        filas.append({
            "paso": etiqueta, "evento": evento, "usuarios": usuarios,
            "pct_continua": pct_continua, "abandonan": abandonan, "pct_abandona": pct_abandona,
        })
    return filas


def _dsn() -> str:
    import streamlit as st
    dsn = st.secrets.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("Falta DATABASE_URL en los secrets de Streamlit Cloud.")
    return dsn


def dist_negocio(fecha_desde: date, fecha_hasta: date) -> dict | None:
    """% de transacciones reales que le corresponde a cada negocio en el rango
    dado -- para prorratear el funnel de la App entre Farmacorp y Amarket (ver
    docstring del módulo, mismo método que Hugo). Lee `resumen_kpis_diarios`
    de Neon/Postgres (mismo criterio que dinex_cloud: no hay sqlite local acá).
    Devuelve {'FA': pct_0_a_1, 'AM': pct_0_a_1, 'trans_fa': N, 'trans_am': N} o
    None si no hay datos de ninguno de los dos en ese rango (evita dividir por
    cero)."""
    import psycopg
    with psycopg.connect(_dsn()) as conn:
        rows = conn.execute(
            """SELECT negocio, SUM(transacciones) trans
               FROM resumen_kpis_diarios WHERE fecha BETWEEN %s AND %s AND negocio IN ('FA','AM')
               GROUP BY negocio""",
            (fecha_desde.isoformat(), fecha_hasta.isoformat()),
        ).fetchall()
    trans = {neg: (n or 0) for neg, n in rows}
    trans_fa, trans_am = trans.get('FA', 0), trans.get('AM', 0)
    total = trans_fa + trans_am
    if not total:
        return None
    return {"FA": trans_fa / total, "AM": trans_am / total, "trans_fa": trans_fa, "trans_am": trans_am}


def fetch_instalaciones(fecha_desde: date, fecha_hasta: date) -> list[dict]:
    """Instalaciones (first_open) vs desinstalaciones (app_remove) de la App,
    por mes, en el rango dado. Solo tiene sentido para la propiedad App (la
    Web no tiene estos eventos). No es un funnel -- son dos conteos de eventos
    independientes por mes, sencillo de propósito."""
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from google.analytics.data_v1beta.types import (
        RunReportRequest, DateRange, Dimension, Metric,
        FilterExpression, Filter, OrderBy,
    )

    client = BetaAnalyticsDataClient(credentials=_get_creds())
    req = RunReportRequest(
        property=f"properties/{PROPIEDADES['App']['id']}",
        dimensions=[Dimension(name="yearMonth"), Dimension(name="eventName")],
        metrics=[Metric(name="eventCount")],
        date_ranges=[DateRange(start_date=fecha_desde.isoformat(), end_date=fecha_hasta.isoformat())],
        dimension_filter=FilterExpression(
            filter=Filter(
                field_name="eventName",
                in_list_filter=Filter.InListFilter(values=[EVENTO_INSTALL, EVENTO_UNINSTALL]),
            )
        ),
        order_bys=[OrderBy(dimension=OrderBy.DimensionOrderBy(dimension_name="yearMonth"))],
    )
    resp = client.run_report(req)

    por_mes: dict[str, dict] = {}
    for row in resp.rows:
        mes, evento = row.dimension_values[0].value, row.dimension_values[1].value
        cnt = int(row.metric_values[0].value)
        por_mes.setdefault(mes, {"mes": mes, "instalaciones": 0, "desinstalaciones": 0})
        if evento == EVENTO_INSTALL:
            por_mes[mes]["instalaciones"] = cnt
        elif evento == EVENTO_UNINSTALL:
            por_mes[mes]["desinstalaciones"] = cnt
    return [por_mes[m] for m in sorted(por_mes)]
