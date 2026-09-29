"""Funnels de Farmacorp — App y Web (para Lu, UX/UI + pedido de Hugo).

Deploy standalone en Streamlit Community Cloud, público (Mariano, 2026-09-29)
-- copia adaptada de dinex_tower/funnel_app.py; ver ga4_fetch.py de esta
misma carpeta para el detalle de qué cambió (credenciales/DB desde secrets
en vez de archivos locales).

3 vistas (pedido de Hugo, 2026-08-31):
1. Funnel de compra -- cada paso dentro de 30 min del anterior (≈ una sola
   visita) -- ver el docstring de ga4_fetch.py para por qué: sin esa
   restricción la conversión sale artificialmente 5 veces más alta, mezclando
   gente que volvió semanas después como si fuera el mismo intento.
2. Funnel de onboarding -- separado del de compra, mismo criterio de ventana.
3. Instalación/desinstalación -- solo App (la Web no tiene estos eventos),
   número simple por mes, no un funnel.
"""
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import ga4_fetch as ga4

st.set_page_config(page_title="Funnels — Farmacorp", page_icon="🛒", layout="wide")

st.markdown("""
<style>
[data-testid="stHeader"] { background:transparent !important; height:0 !important;
  min-height:0 !important; pointer-events:none !important; }
.block-container { padding-top:1.2rem !important; }
</style>
""", unsafe_allow_html=True)

if not ga4.creds_disponibles():
    st.error("Faltan las credenciales de GA4 en los secrets de esta app (`ga4_service_account`).")
    st.stop()


def _num(n):
    """Miles con punto, sin 'k'/'M' de Plotly (formato inglés que confunde en es-BO:
    Plotly mostraba '43.906k' con el punto como si fuera decimal, no separador de miles)."""
    return f"{n:,.0f}".replace(",", ".")


# ── Sidebar: vista + controles según la vista elegida ──────────────────────
with st.sidebar:
    st.header("Vista")
    vista = st.radio(
        "Qué ver", ["Funnel de compra", "Funnel de onboarding", "Instalación/desinstalación"],
        label_visibility="collapsed",
    )

    hoy = date.today()
    meses_es = ["Enero","Febrero","Marzo","Abril","Mayo","Junio","Julio",
                "Agosto","Septiembre","Octubre","Noviembre","Diciembre"]

    if vista in ("Funnel de compra", "Funnel de onboarding"):
        st.header("Propiedad")
        propiedad = st.radio("Ver", list(ga4.PROPIEDADES.keys()), horizontal=True)

        negocio = "Total"
        if propiedad == "App":
            st.header("Negocio")
            negocio = st.radio(
                "Ver", ["Total", "Farmacorp", "Amarket"], horizontal=True,
                help="La App es una sola propiedad de GA4 compartida entre las dos marcas -- "
                     "Farmacorp/Amarket se ESTIMAN prorrateando con el % real de transacciones "
                     "del mismo período (no hay forma de separarlos en GA4 todavía).",
            )

        st.header("Período")
        modo = st.radio("Ver por", ["Día", "Mes"], horizontal=True)
        if modo == "Día":
            dia_sel = st.date_input("Día", value=hoy - timedelta(days=1), max_value=hoy)
            fecha_desde, fecha_hasta = dia_sel, dia_sel
            etiqueta_periodo = dia_sel.strftime("%d/%m/%Y")
        else:
            anos = list(range(hoy.year, hoy.year - 3, -1))
            c1, c2 = st.columns(2)
            ano_sel = c1.selectbox("Año", anos, index=0)
            mes_default = hoy.month - 1 if hoy.month > 1 else 12
            mes_sel = c2.selectbox("Mes", range(1, 13), index=mes_default - 1, format_func=lambda m: meses_es[m-1])
            fecha_desde = date(ano_sel, mes_sel, 1)
            ultimo_dia = (date(ano_sel + 1, 1, 1) if mes_sel == 12 else date(ano_sel, mes_sel + 1, 1)) - timedelta(days=1)
            fecha_hasta = min(ultimo_dia, hoy - timedelta(days=1))  # GA4 no tiene "hoy" completo todavía
            etiqueta_periodo = f"{meses_es[mes_sel-1]} {ano_sel}"
        st.caption(f"Del **{fecha_desde:%d/%m/%Y}** al **{fecha_hasta:%d/%m/%Y}**")

    else:  # Instalación/desinstalación -- solo tiene sentido para la App
        st.caption("Solo App — la Web no tiene eventos de instalar/desinstalar.")
        propiedad = "App"
        negocio = "Total"  # esta vista no prorratea por negocio todavía
        n_meses = st.selectbox("Últimos N meses", [3, 6, 12], index=1)
        ultimo_mes_cerrado = (hoy.replace(day=1) - timedelta(days=1)).replace(day=1)
        fecha_hasta = (hoy.replace(day=1) - timedelta(days=1))  # último día del mes anterior
        m, y = ultimo_mes_cerrado.month, ultimo_mes_cerrado.year
        for _ in range(n_meses - 1):
            m -= 1
            if m == 0:
                m, y = 12, y - 1
        fecha_desde = date(y, m, 1)
        etiqueta_periodo = f"{meses_es[fecha_desde.month-1]} {fecha_desde.year} → {meses_es[fecha_hasta.month-1]} {fecha_hasta.year}"
        st.caption(f"Del **{fecha_desde:%d/%m/%Y}** al **{fecha_hasta:%d/%m/%Y}**")

    consultar = st.button("🔎 Traer datos", type="primary", use_container_width=True)

if consultar:
    st.session_state["vista"] = vista
    st.session_state["propiedad"] = propiedad
    st.session_state["negocio"] = negocio
    st.session_state["fecha_desde"] = fecha_desde
    st.session_state["fecha_hasta"] = fecha_hasta
    st.session_state["etiqueta_periodo"] = etiqueta_periodo
    st.session_state.pop("data", None)
    st.session_state.pop("dist", None)

if "vista" not in st.session_state:
    st.title("🛒 Funnels — Farmacorp")
    st.info("Elegí una vista y período a la izquierda y apretá **Traer datos**.")
    st.stop()

V = st.session_state["vista"]

# ═══════════════════════════════════════════════════════════════════════════
# VISTA: Funnel de compra / Funnel de onboarding
# ═══════════════════════════════════════════════════════════════════════════
if V in ("Funnel de compra", "Funnel de onboarding"):
    tipo = "compra" if V == "Funnel de compra" else "onboarding"
    st.title(f"🛒 {V} — Farmacorp" if tipo == "compra" else f"🚪 {V} — Farmacorp")
    st.caption(
        "Personas únicas que llegaron a cada paso, cada uno dentro de "
        f"{ga4.DENTRO_DE_MIN} minutos del anterior (≈ una sola visita) — "
        "así se mide fricción real de un intento, no gente que volvió semanas después."
    )
    if tipo == "compra":
        st.warning(
            "⚠️ El paso **\"Compró\"** cuenta el evento `purchase` de GA4 — sirve para ver dónde se "
            "cae la gente, **no** como número real de ventas (analytics pierde ventas por bloqueadores "
            "de tracking, fallas de red, o compras por WhatsApp/Contact Center fuera de la app/web). "
            "La venta real sigue el sistema de ventas de Farmacorp, igual que en la Sheets Fuente Datos Nexocorp.",
            icon="⚠️",
        )

    if "data" not in st.session_state:
        with st.spinner(f"Consultando GA4 ({st.session_state['propiedad']})..."):
            try:
                st.session_state["data"] = ga4.fetch_funnel(
                    st.session_state["fecha_desde"], st.session_state["fecha_hasta"],
                    propiedad=st.session_state["propiedad"], tipo=tipo,
                )
            except Exception as e:
                st.error(f"No se pudo traer el funnel de GA4: {e}")
                st.stop()

    filas = st.session_state["data"]
    negocio_sel = st.session_state.get("negocio", "Total")
    titulo_sub = f"{st.session_state['propiedad']} — Período: {st.session_state['etiqueta_periodo']}"

    # Prorrateo Farmacorp/Amarket -- ver docstring de ga4.dist_negocio(). Aplicar
    # el mismo % a TODOS los pasos no cambia los % entre pasos (se cancela), solo
    # hay que escalar "usuarios"/"abandonan".
    if negocio_sel != "Total" and st.session_state["propiedad"] == "App":
        if "dist" not in st.session_state:
            st.session_state["dist"] = ga4.dist_negocio(
                st.session_state["fecha_desde"], st.session_state["fecha_hasta"]
            )
        dist = st.session_state["dist"]
        if dist is None:
            st.warning(
                "No hay transacciones registradas en este período para calcular el prorrateo "
                "Farmacorp/Amarket — mostrando el Total sin dividir."
            )
        else:
            clave = "FA" if negocio_sel == "Farmacorp" else "AM"
            pct = dist[clave]
            filas = [
                {**f, "usuarios": round(f["usuarios"] * pct),
                 "abandonan": round(f["abandonan"] * pct) if f["abandonan"] is not None else None}
                for f in filas
            ]
            titulo_sub = f"{negocio_sel} (estimado) — Período: {st.session_state['etiqueta_periodo']}"
            st.info(
                f"📐 **Estimado** — de las transacciones reales en este período, "
                f"{pct*100:.1f}% son {negocio_sel} ({dist['trans_fa']:,} Farmacorp / {dist['trans_am']:,} "
                f"Amarket, total {dist['trans_fa']+dist['trans_am']:,}). Ese mismo % se aplicó a cada "
                f"paso del funnel de la App — no es un dato directo de GA4 (no distingue negocio), "
                f"mismo método que ya usa Hugo en la Sheets Fuente Datos Nexocorp.".replace(",", ".")
            )

    st.subheader(titulo_sub)

    if not filas or not filas[0]["usuarios"]:
        st.warning("Sin datos para este período.")
        st.stop()

    etiquetas = [f"{f['paso']} ({f['evento']})" for f in filas]
    valores = [f["usuarios"] for f in filas]

    textos = []
    for i, v in enumerate(valores):
        pct_inicial = v / valores[0] * 100 if valores[0] else 0
        pct_previo = (v / valores[i - 1] * 100) if i > 0 and valores[i - 1] else 100
        textos.append(f"  {_num(v)}  ·  {pct_inicial:.0f}% del inicial  ·  {pct_previo:.0f}% del anterior")

    fig = go.Figure(go.Bar(
        y=etiquetas, x=valores, orientation="h",
        text=textos, textposition="outside",
        marker={"color": "#0d6e4e"},
        cliponaxis=False,
    ))
    fig.update_yaxes(autorange="reversed")
    fig.update_xaxes(visible=False, range=[0, max(valores) * 1.45])
    fig.update_layout(
        height=420, margin=dict(l=10, r=140, t=10, b=10), font=dict(size=14),
        bargap=0.35, plot_bgcolor="rgba(0,0,0,0)",
    )
    st.plotly_chart(fig, use_container_width=True)

    tot_inicial = valores[0]
    tabla = pd.DataFrame([
        {
            "Paso": f["paso"],
            "Evento (GA4)": f["evento"],
            "Personas": f["usuarios"],
            "% del total": f["usuarios"] / tot_inicial * 100 if tot_inicial else 0,
            "Siguió al próximo paso": f"{f['pct_continua']*100:.1f}%" if f["pct_continua"] is not None else "—",
            "Se fue acá": f["abandonan"] if f["abandonan"] is not None else "—",
        }
        for f in filas
    ])
    st.dataframe(
        tabla, use_container_width=True, hide_index=True,
        column_config={
            "Personas": st.column_config.NumberColumn(format="%d"),
            "% del total": st.column_config.NumberColumn(format="%.1f%%"),
        },
    )

    fugas = [
        (filas[i]["paso"], filas[i+1]["paso"], filas[i]["abandonan"], filas[i]["pct_abandona"])
        for i in range(len(filas) - 1)
        if filas[i]["abandonan"] is not None
    ]
    if fugas:
        peor = max(fugas, key=lambda x: x[2])
        st.info(
            f"📉 **Mayor fuga:** entre *\"{peor[0]}\"* y *\"{peor[1]}\"* — se pierden "
            f"**{peor[2]:,}** personas ({peor[3]*100:.1f}%)".replace(",", ".")
        )

    conv_total = valores[-1] / tot_inicial * 100 if tot_inicial else 0
    ultimo_paso = filas[-1]["paso"]
    st.caption(f"Conversión de punta a punta ({filas[0]['paso']} → {ultimo_paso}, mismo intento): **{conv_total:.2f}%**")

# ═══════════════════════════════════════════════════════════════════════════
# VISTA: Instalación / desinstalación (solo App)
# ═══════════════════════════════════════════════════════════════════════════
else:
    st.title("📲 Instalación / desinstalación — App Farmacorp")
    st.caption(
        "`first_open` (primera apertura, proxy de instalación) vs `app_remove` (desinstalación), "
        "por mes. No es un funnel — son dos eventos independientes en el tiempo, no tiene sentido "
        "medir conversión entre ellos."
    )

    if "data" not in st.session_state:
        with st.spinner("Consultando GA4 (App)..."):
            try:
                st.session_state["data"] = ga4.fetch_instalaciones(
                    st.session_state["fecha_desde"], st.session_state["fecha_hasta"]
                )
            except Exception as e:
                st.error(f"No se pudo traer los datos de GA4: {e}")
                st.stop()

    filas = st.session_state["data"]
    st.subheader(f"Período: {st.session_state['etiqueta_periodo']}")

    if not filas:
        st.warning("Sin datos para este período.")
        st.stop()

    meses_es = ["Ene","Feb","Mar","Abr","May","Jun","Jul","Ago","Sep","Oct","Nov","Dic"]
    def _fmt_mes(ym):  # "202608" -> "Ago 2026"
        return f"{meses_es[int(ym[4:6]) - 1]} {ym[:4]}"

    meses = [_fmt_mes(f["mes"]) for f in filas]
    instal = [f["instalaciones"] for f in filas]
    desinstal = [f["desinstalaciones"] for f in filas]

    fig = go.Figure()
    fig.add_bar(name="Instalaciones (first_open)", x=meses, y=instal, marker_color="#0d6e4e")
    fig.add_bar(name="Desinstalaciones (app_remove)", x=meses, y=desinstal, marker_color="#c0392b")
    fig.update_layout(
        barmode="group", height=420, margin=dict(l=10, r=10, t=30, b=10),
        font=dict(size=14), legend=dict(orientation="h", yanchor="bottom", y=1.02),
        plot_bgcolor="rgba(0,0,0,0)",
    )
    st.plotly_chart(fig, use_container_width=True)

    tabla = pd.DataFrame([
        {
            "Mes": _fmt_mes(f["mes"]),
            "Instalaciones": f["instalaciones"],
            "Desinstalaciones": f["desinstalaciones"],
            "Neto": f["instalaciones"] - f["desinstalaciones"],
        }
        for f in filas
    ])
    st.dataframe(tabla, use_container_width=True, hide_index=True)

    tot_i, tot_d = sum(instal), sum(desinstal)
    st.caption(
        f"Total del período: **{_num(tot_i)}** instalaciones, **{_num(tot_d)}** desinstalaciones "
        f"(neto **{_num(tot_i - tot_d)}**)."
    )
