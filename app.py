import streamlit as st
import pandas as pd
import numpy as np
import pydeck as pdk
import altair as alt
import requests
import hashlib
import math
import re
from pathlib import Path
from ortools.constraint_solver import routing_enums_pb2, pywrapcp

# ============================================================
# CONFIGURACIÓN GENERAL
# ============================================================
st.set_page_config(
    page_title="Baseline + Optimizador PM",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
.block-container{padding-top:1rem;padding-bottom:1rem;}
[data-testid="stHeader"]{height:0rem;}
.main-title{font-size:2.2rem;font-weight:850;margin-bottom:.1rem;}
.subtitle{color:#667085;font-size:1rem;margin-bottom:1rem;}
.section-title{font-size:1.35rem;font-weight:800;margin-top:.5rem;margin-bottom:.4rem;}
.section-subtitle{color:#667085;font-size:.95rem;margin-bottom:.8rem;}
.route-card{border:1px solid #EAECF0;border-radius:14px;padding:12px;background:#FFF;margin-bottom:10px;}
.small-note{color:#667085;font-size:.85rem;}
.day-card{border:1px solid #D0D5DD;border-radius:16px;padding:16px 18px;background:#F9FAFB;margin:.4rem 0 1rem 0;}
.day-card-title{font-size:.85rem;color:#667085;font-weight:700;text-transform:uppercase;letter-spacing:.04em;margin-bottom:.2rem;}
.day-card-date{font-size:1.45rem;font-weight:850;color:#101828;margin-bottom:.25rem;}
.day-card-detail{font-size:.95rem;color:#475467;}
.metric-card-soft{border:1px solid #EAECF0;border-radius:14px;padding:10px 12px;background:#FFFFFF;}
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="main-title">Baseline Operacional + Optimizador PM</div>', unsafe_allow_html=True)
st.markdown('<div class="subtitle">Comparación automática: Baseline vs Optimización de ruta vs Optimización de capacidades</div>', unsafe_allow_html=True)

HUB_LAT = -33.43291
HUB_LON = -70.797027
HUB_COORD = (HUB_LAT, HUB_LON)
TIEMPO_MAX_RUTA_MIN = 480
HORA_CORTE_HUB_MIN = 14 * 60
# Bloques operativos flexibles: no obliga volver exactamente a las 14:00.
# AM debe cerrar cerca del corte; PM puede partir antes/después del corte según ventana.
BLOQUE_AM_START_MIN = 6 * 60
BLOQUE_AM_END_MIN = 15 * 60
BLOQUE_PM_START_MIN = 12 * 60
BLOQUE_PM_END_MIN = 22 * 60
TIEMPO_SERVICIO_BASE = 15
TIEMPO_SERVICIO_OS = 0.1
DEFAULT_CAPACIDAD = 20
DICRUTA_DEFAULT_PATH = Path("dicruta.xlsx")

# Capacidad del diccionario viene en m3.
# El volumen operativo del optimizador se calcula en m3 desde kilo mayor.
# Conversión usada: m3 = kilo_mayor / 250, porque kilo_mayor = cm3 / 4000.

# ============================================================
# UTILIDADES
# ============================================================
def normalizar_ruta(valor):
    if pd.isna(valor):
        return np.nan
    return str(valor).upper().strip().replace(".0", "")

def buscar_columna(df, opciones):
    cols = {str(c).lower().strip(): c for c in df.columns}
    for op in opciones:
        if op.lower() in cols:
            return cols[op.lower()]
    return None

def buscar_columna_contiene(df, patrones):
    for c in df.columns:
        cl = str(c).lower().strip()
        if any(p.lower() in cl for p in patrones):
            return c
    return None

def color_from_text(text):
    h = hashlib.md5(str(text).encode()).hexdigest()
    return [int(h[0:2],16), int(h[2:4],16), int(h[4:6],16), 220]

def color_rgba_css(color):
    return f"rgba({color[0]},{color[1]},{color[2]},{color[3]/255:.2f})"

def num(v):
    if pd.isna(v): return "-"
    try: return f"{int(round(float(v))):,}".replace(",", ".")
    except Exception: return str(v)

def dec(v):
    if pd.isna(v): return "-"
    try: return f"{float(v):,.1f}".replace(",", ".")
    except Exception: return str(v)

def kilo_mayor_a_m3(valor):
    """Convierte kilo mayor a m3 para comparar contra capacidad vehicular en m3.

    El kilo mayor viene calculado como ancho * largo * alto / 4000,
    usando dimensiones en cm. Por eso:
        m3 = kilo_mayor * 4000 / 1.000.000 = kilo_mayor / 250
    """
    return (pd.to_numeric(valor, errors="coerce").fillna(0) / 250).clip(lower=0)

def ajustar_volumen_m3(kilo_mayor, q_os, capmax_por_os=None, umbral_m3_por_os=3.0, m3_estandar_por_os=0.33):
    """Ajusta cubicaciones erráticas usando Capmax del diccionario de rutas.

    1) Convierte kilo mayor a m3.
    2) Calcula m3 por OS: volumen_m3 / q_os.
    3) Si m3_por_os supera el umbral, reemplaza el volumen total por:
       q_os * Capmax de la ruta.
       Si la ruta no tiene Capmax válido, usa q_os * m3_estandar_por_os como respaldo.
    """
    volumen_original = kilo_mayor_a_m3(kilo_mayor)
    os = pd.to_numeric(q_os, errors="coerce").fillna(1)
    os = os.mask(os <= 0, 1)

    if capmax_por_os is None:
        capmax = pd.Series(m3_estandar_por_os, index=volumen_original.index)
    else:
        capmax = pd.to_numeric(capmax_por_os, errors="coerce")
        if not isinstance(capmax, pd.Series):
            capmax = pd.Series(capmax, index=volumen_original.index)
        capmax = capmax.reindex(volumen_original.index).fillna(m3_estandar_por_os)
        capmax = capmax.mask(capmax <= 0, m3_estandar_por_os)

    m3_por_os = (volumen_original / os).replace([np.inf, -np.inf], 0).fillna(0)
    volumen_ajustado = m3_por_os > umbral_m3_por_os
    volumen_final = volumen_original.mask(volumen_ajustado, os * capmax).clip(lower=0)

    return pd.DataFrame({
        "volumen_original_m3": volumen_original,
        "volumen_m3": volumen_final,
        "m3_por_os": m3_por_os,
        "capmax_usado": capmax,
        "volumen_ajustado": volumen_ajustado
    })

def pct(v):
    if pd.isna(v): return "-"
    return f"{float(v):.1f}%"

def minutes_to_time(minutes):
    if pd.isna(minutes): return "-"
    h = int(minutes // 60) % 24
    m = int(minutes % 60)
    return f"{h:02d}:{m:02d}"

def time_to_minutes(t):
    if pd.isna(t): return None
    if hasattr(t, "hour"):
        return int(t.hour) * 60 + int(t.minute)
    try:
        tt = pd.to_datetime(str(t), errors="coerce").time()
        return int(tt.hour) * 60 + int(tt.minute)
    except Exception:
        return None

def extraer_rango_ventana(valor):
    if pd.isna(valor): return (pd.NaT, pd.NaT)
    txt = str(valor).lower().strip()
    txt = txt.replace("hrs", "").replace("hr", "")
    txt = txt.replace("desde", "").replace("hasta", "-")
    txt = txt.replace(" a ", "-").replace("–", "-").replace("—", "-")
    partes = re.findall(r"\d{1,2}(?::\d{2})?", txt)
    if len(partes) < 2: return (pd.NaT, pd.NaT)
    h_ini, h_fin = partes[0], partes[1]
    if ":" not in h_ini: h_ini += ":00"
    if ":" not in h_fin: h_fin += ":00"
    return (pd.to_datetime(h_ini, errors="coerce").time(), pd.to_datetime(h_fin, errors="coerce").time())

def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dl/2)**2
    return 2*r*math.atan2(math.sqrt(a), math.sqrt(1-a))

# ============================================================
# FILTROS TIPO EXCEL
# ============================================================
def _filter_signature(options):
    return hashlib.md5("|".join([str(o) for o in options]).encode()).hexdigest()[:8]

def excel_filter(label, options, key, default=None):
    """Filtro tipo Excel que se resetea cuando cambia el universo de opciones.
    Evita que queden checkboxes antiguos pegados al cambiar fecha/clase/tipo.
    """
    options = sorted([o for o in options if pd.notna(o)])
    if default is None:
        default = options
    default = [o for o in default if o in options]
    sig = _filter_signature(options)
    selected_key = f"{key}_selected"
    sig_key = f"{key}_options_sig"

    if sig_key not in st.session_state or st.session_state[sig_key] != sig:
        st.session_state[selected_key] = list(default)
        st.session_state[sig_key] = sig

    # limpia selecciones que ya no existen
    st.session_state[selected_key] = [o for o in st.session_state.get(selected_key, []) if o in options]

    resumen = "Todos" if len(st.session_state[selected_key]) == len(options) else f"{len(st.session_state[selected_key])}/{len(options)}"
    with st.popover(f"🔽 {label}: {resumen}", use_container_width=True):
        search = st.text_input("Buscar", key=f"{key}_{sig}_search")
        visibles = [o for o in options if search.lower() in str(o).lower()] if search else options
        c1, c2 = st.columns(2)
        if c1.button("Seleccionar todo", key=f"{key}_{sig}_all"):
            st.session_state[selected_key] = list(options)
            st.rerun()
        if c2.button("Limpiar", key=f"{key}_{sig}_clear"):
            st.session_state[selected_key] = []
            st.rerun()
        st.caption(f"Seleccionados: {len(st.session_state[selected_key])} de {len(options)}")
        for op in visibles:
            op_hash = hashlib.md5(str(op).encode()).hexdigest()[:10]
            checked = op in st.session_state[selected_key]
            val = st.checkbox(str(op), value=checked, key=f"{key}_{sig}_chk_{op_hash}")
            if val and op not in st.session_state[selected_key]:
                st.session_state[selected_key].append(op)
            if not val and op in st.session_state[selected_key]:
                st.session_state[selected_key].remove(op)
    return st.session_state[selected_key]

def excel_single_filter(label, options, key, default=None):
    """Selector único que se resetea si cambian las opciones disponibles."""
    options = sorted([o for o in options if pd.notna(o)])
    if not options:
        return None
    if default is None or default not in options:
        default = options[0]
    sig = _filter_signature(options)
    selected_key = f"{key}_selected_single"
    sig_key = f"{key}_options_sig_single"

    if sig_key not in st.session_state or st.session_state[sig_key] != sig or st.session_state.get(selected_key) not in options:
        st.session_state[selected_key] = default
        st.session_state[sig_key] = sig

    with st.popover(f"🔽 {label}: {st.session_state[selected_key]}", use_container_width=True):
        search = st.text_input("Buscar", key=f"{key}_{sig}_search_single")
        visibles = [o for o in options if search.lower() in str(o).lower()] if search else options
        if visibles:
            actual = st.session_state[selected_key]
            idx = visibles.index(actual) if actual in visibles else 0
            seleccionado = st.radio("Selecciona", visibles, index=idx, key=f"{key}_{sig}_radio")
            st.session_state[selected_key] = seleccionado
    return st.session_state[selected_key]

# ============================================================
# CARGA Y PREPARACIÓN DE DATA
# ============================================================
@st.cache_data(show_spinner=False)
def read_excel_any(file_or_path):
    xl = pd.ExcelFile(file_or_path)
    # usa la hoja con más columnas útiles
    best = None
    best_score = -1
    for sh in xl.sheet_names:
        df = pd.read_excel(file_or_path, sheet_name=sh)
        score = sum(str(c).lower() in ["ruta","reserva","a_operacion","solo_fch","capacidad"] for c in df.columns)
        if score > best_score:
            best, best_score = df, score
    return best

@st.cache_data(show_spinner=False)
def cargar_dicruta(file_or_path):
    dic = read_excel_any(file_or_path).copy()
    dic.columns = dic.columns.str.strip()
    col_ruta = buscar_columna(dic, ["ruta"])
    col_cap = buscar_columna(dic, ["capacidad", "capacidad_max", "capacidad máxima", "ocupacion_max", "ocupación máxima"])
    col_tipo_flota = buscar_columna(dic, ["tipo flota", "tipo_flota"])
    col_capmax = buscar_columna(dic, ["capmax", "cap_max", "cap max", "capacidad_os", "capacidad os", "prom_os_m3", "prom os m3"])
    col_horario = buscar_columna(dic, ["horario", "hora", "hora_inicio", "hora inicio", "inicio ruta", "inicio_ruta"])
    if col_ruta is None or col_cap is None:
        raise ValueError("El diccionario de rutas debe tener columnas Ruta y Capacidad.")
    out = dic.copy()
    out["ruta"] = out[col_ruta].apply(normalizar_ruta)
    out["capacidad"] = pd.to_numeric(out[col_cap], errors="coerce").fillna(DEFAULT_CAPACIDAD)
    out["tipo_flota"] = out[col_tipo_flota].astype(str) if col_tipo_flota else "SIN TIPO"
    out["capmax"] = pd.to_numeric(out[col_capmax], errors="coerce") if col_capmax else np.nan
    # Horario define desde qué minuto del día puede activarse la ruta en escenarios optimizados.
    out["horario_min"] = out[col_horario].apply(time_to_minutes) if col_horario else np.nan
    out["horario_txt"] = out["horario_min"].apply(lambda x: minutes_to_time(x) if pd.notna(x) else "Sin restricción")
    return out.dropna(subset=["ruta"])[["ruta", "capacidad", "tipo_flota", "capmax", "horario_min", "horario_txt"]].drop_duplicates("ruta")

@st.cache_data(show_spinner=False)
def preparar_data(peu_raw, accesos_raw):
    peu = peu_raw.copy(); accesos = accesos_raw.copy()
    peu.columns = peu.columns.str.strip(); accesos.columns = accesos.columns.str.strip()

    col_ruta_peu = buscar_columna(peu, ["ruta", "tab_ruta", "bdga_cdg"])
    col_ruta_acc = buscar_columna(accesos, ["ruta"])
    col_clase = buscar_columna(peu, ["clase_ruta", "clase de ruta", "base2"])
    col_tipo = buscar_columna(peu, ["tipo_ruta", "tipo de ruta", "base1"])
    col_fecha_peu = buscar_columna(peu, ["a_operacion", "fecha_operacion", "fecha de operación"])
    col_hora_peu = buscar_columna(peu, ["hora_gestion", "hora de gestion", "hora de gestión"])
    col_fecha_acc = buscar_columna(accesos, ["solo_fch", "solo fecha", "fecha_ingreso", "fecha de ingreso"])
    col_hora_acc = buscar_columna(accesos, ["hora_entrada", "hora entrada", "hora de entrada"])
    col_evento = buscar_columna(peu, ["evento"])
    col_ventana = buscar_columna(peu, ["ventana_horaria", "ventana horaria", "ventana"])
    col_inicio_vh = buscar_columna(peu, ["inicio_vh", "inicio ventana", "ventana_inicio"])
    col_fin_vh = buscar_columna(peu, ["fin_vh", "fin ventana", "ventana_fin"])
    col_reserva = buscar_columna(peu, ["reserva"])
    col_comuna = buscar_columna(peu, ["comuna", "comuna_destino", "comuna destino"])
    col_q_os = buscar_columna(peu, ["q_os", "cantidad_os", "cantidad de os"])
    col_km = buscar_columna(peu, ["kilomayor", "kilo_mayor", "kilo mayor", "kmayor", "kmay_ajustado"])
    col_piezas = buscar_columna(peu, ["q_piezas", "piezas", "cantidad_piezas", "volumen"])

    faltantes = []
    for nombre, col in {
        "ruta PU": col_ruta_peu, "ruta accesos": col_ruta_acc, "clase/base2 PU": col_clase,
        "fecha PU": col_fecha_peu, "hora PU": col_hora_peu,
        "fecha accesos": col_fecha_acc, "hora accesos": col_hora_acc, "evento PU": col_evento,
        "reserva PU": col_reserva
    }.items():
        if col is None: faltantes.append(nombre)
    if faltantes: return None, None, None, faltantes

    peu["ruta"] = peu[col_ruta_peu].apply(normalizar_ruta)
    accesos["ruta"] = accesos[col_ruta_acc].apply(normalizar_ruta)
    peu["clase_ruta"] = peu[col_clase].astype(str).str.upper().str.strip()
    peu["tipo_ruta"] = peu[col_tipo].astype(str).str.upper().str.strip() if col_tipo else "SIN TIPO"
    peu["comuna"] = peu[col_comuna].astype(str).str.upper().str.strip() if col_comuna else "SIN COMUNA"
    peu["fecha_operacion"] = pd.to_datetime(peu[col_fecha_peu], errors="coerce").dt.date
    accesos["solo_fch"] = pd.to_datetime(accesos[col_fecha_acc], errors="coerce").dt.date
    peu["datetime_gestion"] = pd.to_datetime(peu["fecha_operacion"].astype(str) + " " + peu[col_hora_peu].astype(str), errors="coerce")
    accesos["datetime_ingreso"] = pd.to_datetime(accesos["solo_fch"].astype(str) + " " + accesos[col_hora_acc].astype(str), errors="coerce")

    lat_col = buscar_columna_contiene(peu, ["lat"])
    lon_col = buscar_columna_contiene(peu, ["lon"])
    for c in [lat_col, lon_col, col_q_os, col_km, col_piezas]:
        if c: peu[c] = pd.to_numeric(peu[c], errors="coerce")
    for c in [col_q_os, col_km, col_piezas]:
        if c: peu[c] = peu[c].fillna(0)

    peu["evento_norm"] = peu[col_evento].astype(str).str.upper().str.strip()
    peu["gestion_correcta"] = peu["evento_norm"].eq("PU")
    peu["es_excepcion"] = ~peu["gestion_correcta"]
    peu["sin_geo"] = peu[lat_col].isna() | peu[lon_col].isna() if lat_col and lon_col else True

    if col_inicio_vh and col_fin_vh:
        peu["ventana_inicio"] = pd.to_datetime(peu[col_inicio_vh].astype(str), errors="coerce").dt.time
        peu["ventana_fin"] = pd.to_datetime(peu[col_fin_vh].astype(str), errors="coerce").dt.time
    elif col_ventana:
        rangos = peu[col_ventana].apply(extraer_rango_ventana)
        peu["ventana_inicio"] = rangos.apply(lambda x: x[0])
        peu["ventana_fin"] = rangos.apply(lambda x: x[1])
    else:
        peu["ventana_inicio"] = pd.NaT; peu["ventana_fin"] = pd.NaT

    def cumple_vh(row):
        if pd.isna(row["ventana_inicio"]) or pd.isna(row["ventana_fin"]) or pd.isna(row["datetime_gestion"]):
            return np.nan
        fecha = row["datetime_gestion"].date()
        ini = pd.Timestamp.combine(fecha, row["ventana_inicio"])
        fin = pd.Timestamp.combine(fecha, row["ventana_fin"])
        if row["ventana_inicio"] > row["ventana_fin"]: fin += pd.Timedelta(days=1)
        return ini <= row["datetime_gestion"] <= fin

    peu["cumple_ventana"] = peu.apply(cumple_vh, axis=1)
    peu["estado_ventana"] = np.where(peu["cumple_ventana"].eq(True), "Cumple", np.where(peu["cumple_ventana"].eq(False), "No cumple", "Sin ventana válida"))

    peu = peu.dropna(subset=["fecha_operacion", "datetime_gestion", "ruta"])
    accesos = accesos.dropna(subset=["solo_fch", "datetime_ingreso", "ruta"])
    cols = dict(col_reserva=col_reserva, col_comuna=col_comuna, col_q_os=col_q_os, col_km=col_km, col_piezas=col_piezas, lat_col=lat_col, lon_col=lon_col)
    return peu, accesos, cols, []

@st.cache_data(show_spinner=False)
def asignar_vueltas_por_cierre(peu_df, accesos_df):
    peu_df = peu_df.copy(); accesos_df = accesos_df.copy(); resultados = []
    for (fecha, ruta), g in peu_df.groupby(["fecha_operacion", "ruta"], dropna=False):
        g = g.sort_values("datetime_gestion").copy()
        acc = accesos_df[(accesos_df["solo_fch"] == fecha) & (accesos_df["ruta"] == ruta)].sort_values("datetime_ingreso")
        cierres = acc["datetime_ingreso"].dropna().to_list()
        if not cierres:
            g["vuelta"] = np.nan; g["datetime_cierre_vuelta"] = pd.NaT; g["estado_asignacion_vuelta"] = "Sin ingreso HJ"; resultados.append(g); continue
        cierres_np = np.array(cierres, dtype="datetime64[ns]")
        vueltas=[]; cierre_asig=[]; estados=[]
        for dt in g["datetime_gestion"]:
            idx = np.searchsorted(cierres_np, np.datetime64(dt), side="left")
            vueltas.append(idx+1)
            if idx < len(cierres):
                cierre_asig.append(cierres[idx]); estados.append("Vuelta cerrada por ingreso HJ")
            else:
                cierre_asig.append(pd.NaT); estados.append("Vuelta posterior al último ingreso HJ")
        g["vuelta"] = vueltas; g["datetime_cierre_vuelta"] = cierre_asig; g["estado_asignacion_vuelta"] = estados
        resultados.append(g)
    return pd.concat(resultados, ignore_index=True) if resultados else peu_df

# ============================================================
# OPTIMIZADOR
# ============================================================
def fallback_matrix(coords, speed_kmh=28):
    n = len(coords); matrix = [[0]*n for _ in range(n)]
    for i,a in enumerate(coords):
        for j,b in enumerate(coords):
            if i == j: continue
            km = haversine_km(a[0], a[1], b[0], b[1])
            matrix[i][j] = int(math.ceil((km*1.35)/speed_kmh*60))
    return matrix

@st.cache_data(show_spinner=False)
def get_vial_matrix(coords_tuple, usar_osrm=True):
    coords = list(coords_tuple)
    if not usar_osrm or len(coords) > 90:
        return fallback_matrix(coords), "fallback"
    coord_txt = ";".join([f"{lon},{lat}" for lat, lon in coords])
    url = f"https://router.project-osrm.org/table/v1/driving/{coord_txt}?annotations=duration"
    try:
        r = requests.get(url, timeout=30)
        data = r.json()
        if data.get("durations"):
            matriz = np.array(data["durations"], dtype=float) / 60.0
            return np.ceil(np.nan_to_num(matriz, nan=9999)).astype(int).tolist(), "osrm"
    except Exception:
        pass
    return fallback_matrix(coords), "fallback"

@st.cache_data(show_spinner=False)
def get_route_geometry(coords_tuple, usar_osrm=True):
    coords = list(coords_tuple)
    if not usar_osrm or len(coords) < 2:
        return [[lat, lon] for lat, lon in coords], False, sum(haversine_km(a[0],a[1],b[0],b[1])*1.35 for a,b in zip(coords[:-1], coords[1:]))
    coords_use = coords
    if len(coords_use) > 25:
        idx = np.linspace(0, len(coords_use)-1, 25).astype(int)
        coords_use = [coords_use[i] for i in idx]
    coord_txt = ";".join([f"{lon},{lat}" for lat, lon in coords_use])
    url = f"https://router.project-osrm.org/route/v1/driving/{coord_txt}"
    try:
        r = requests.get(url, params={"overview":"full", "geometries":"geojson", "steps":"false"}, timeout=15)
        routes = r.json().get("routes", [])
        if routes:
            geom = [[p[1], p[0]] for p in routes[0]["geometry"]["coordinates"]]
            return geom, True, routes[0].get("distance", 0)/1000
    except Exception:
        pass
    return [[lat, lon] for lat, lon in coords], False, sum(haversine_km(a[0],a[1],b[0],b[1])*1.35 for a,b in zip(coords[:-1], coords[1:]))

def preparar_puntos(df, cols, max_capacidad=None, dicruta=None):
    lat_col, lon_col = cols["lat_col"], cols["lon_col"]
    col_reserva, col_q_os, col_km, col_piezas = cols["col_reserva"], cols["col_q_os"], cols["col_km"], cols["col_piezas"]
    if not lat_col or not lon_col: return pd.DataFrame()
    d = df.dropna(subset=[lat_col, lon_col]).copy()
    d = d[d["evento_norm"].eq("PU")].copy()
    if d.empty: return pd.DataFrame()
    d["id_punto_opt"] = d[col_reserva].astype(str) if col_reserva else d[lat_col].round(6).astype(str)+"_"+d[lon_col].round(6).astype(str)
    d["_os"] = pd.to_numeric(d[col_q_os], errors="coerce").fillna(1) if col_q_os else 1
    d.loc[d["_os"] <= 0, "_os"] = 1
    d["_kilo"] = pd.to_numeric(d[col_km], errors="coerce").fillna(0).clip(lower=0) if col_km else 0

    # Volumen/capacidad: la demanda se convierte desde kilo mayor a m3.
    # Si m3 por OS supera 3.0, se corrige usando Capmax de la ruta en dicruta.
    capmax_map = dicruta.set_index("ruta")["capmax"].to_dict() if dicruta is not None and "capmax" in dicruta.columns else {}
    d["_capmax_ruta"] = d["ruta"].map(capmax_map)
    if col_km:
        ajuste_vol = ajustar_volumen_m3(d[col_km], d["_os"], capmax_por_os=d["_capmax_ruta"], umbral_m3_por_os=3.0, m3_estandar_por_os=0.33)
        d["_volumen_original"] = ajuste_vol["volumen_original_m3"]
        d["_volumen"] = ajuste_vol["volumen_m3"]
        d["_m3_por_os"] = ajuste_vol["m3_por_os"]
        d["_capmax_usado"] = ajuste_vol["capmax_usado"]
        d["_volumen_ajustado"] = ajuste_vol["volumen_ajustado"]
    else:
        d["_volumen_original"] = 0
        d["_volumen"] = 0
        d["_m3_por_os"] = 0
        d["_capmax_usado"] = d["_capmax_ruta"].fillna(0)
        d["_volumen_ajustado"] = False
    group_cols = ["id_punto_opt", lat_col, lon_col, "ventana_inicio", "ventana_fin"]
    opt = d.groupby(group_cols, dropna=False).agg(
        os=("_os", "sum"),
        volumen=("_volumen", "sum"),
        volumen_original=("_volumen_original", "sum"),
        kilo_mayor=("_kilo", "sum"),
        m3_por_os_max=("_m3_por_os", "max"),
        capmax_usado=("_capmax_usado", "max"),
        volumen_ajustado=("_volumen_ajustado", "max"),
        datetime_gestion=("datetime_gestion", "min"), ruta_original=("ruta", "first"), vuelta_original=("vuelta", "first")
    ).reset_index().rename(columns={lat_col:"lat", lon_col:"lon"})
    opt["tw_start"] = opt["ventana_inicio"].apply(lambda x: time_to_minutes(x) if pd.notna(x) else None)
    opt["tw_end"] = opt["ventana_fin"].apply(lambda x: time_to_minutes(x) if pd.notna(x) else None)
    # ventana obligatoria; cuando no existe, se usa todo el día para no perder demanda
    opt["tw_start"] = opt["tw_start"].fillna(0).astype(int)
    opt["tw_end"] = opt["tw_end"].fillna(1439).astype(int)
    mask = opt["tw_start"] > opt["tw_end"]
    opt.loc[mask, ["tw_start", "tw_end"]] = [0, 1439]

    # Sin escalamiento: volumen y capacidad están en la misma unidad (m3).
    opt["factor_escala_capacidad"] = 1.0

    return opt.reset_index(drop=True)

def construir_flotas_baseline(baseline, dicruta):
    rutas = sorted(baseline["ruta"].dropna().unique())
    cap_map = dicruta.set_index("ruta")["capacidad"].to_dict()
    horario_map = dicruta.set_index("ruta")["horario_min"].to_dict() if "horario_min" in dicruta.columns else {}
    rows = []
    for r in rutas:
        rows.append({"vehiculo_base": r, "capacidad": float(cap_map.get(r, DEFAULT_CAPACIDAD)), "horario_min": horario_map.get(r, np.nan)})
    return pd.DataFrame(rows)

def construir_flotas_capacidades(flota_baseline, demanda_total=0):
    """
    Optimización de capacidades v9.

    Regla operacional corregida:
    - NO usa todo el diccionario de rutas.
    - NO trae camiones/rutas que no participaron en el día.
    - Usa el mismo universo de vehículos/rutas del baseline del día y permite usar menos.
    - Prioriza los vehículos de mayor capacidad DENTRO de la flota usada ese día.

    Esto evita que el escenario active vehículos gigantes del diccionario y baje
    artificialmente el factor de ocupación.
    """
    flota = flota_baseline.copy()
    if flota.empty:
        flota = pd.DataFrame([{"vehiculo_base": f"CAP-{i+1}", "capacidad": DEFAULT_CAPACIDAD} for i in range(max(1, math.ceil(demanda_total/DEFAULT_CAPACIDAD)))])
    flota["capacidad"] = pd.to_numeric(flota["capacidad"], errors="coerce").fillna(DEFAULT_CAPACIDAD)
    return flota.sort_values(["capacidad", "vehiculo_base"], ascending=[False, True]).reset_index(drop=True)

@st.cache_data(show_spinner=False)

def optimizar_cached(puntos, flota, escenario, usar_osrm_matrix=True, usar_osrm_geometry=True):
    """
    Optimizador robusto AM/PM.
    1) Intenta OR-Tools si el tamaño es abordable.
    2) Para días grandes o si OR-Tools queda sin solución, usa una heurística constructiva
       por bloques AM/PM para que el aplicativo siempre genere escenarios comparables.
    """
    if puntos.empty or flota.empty:
        return [], pd.DataFrame(), pd.DataFrame(), {"status":"sin_puntos"}

    puntos = puntos.reset_index(drop=True).copy()

    def construir_vehiculos(flota_base):
        vehs = []
        fb = flota_base.copy()
        # En optimización de capacidades la flota ya viene reducida al universo baseline
        # y ordenada por mayor capacidad disponible dentro de ese mismo día.
        # En optimización de ruta respetamos la nómina completa del baseline.
        if "capacidades" in str(escenario).lower():
            fb = fb.sort_values(["capacidad", "vehiculo_base"], ascending=[False, True])
        for _, row in fb.iterrows():
            cap = float(max(0.001, row["capacidad"]))
            horario_min = row.get("horario_min", np.nan)
            horario_min = int(horario_min) if pd.notna(horario_min) else None
            am_start = max(BLOQUE_AM_START_MIN, horario_min) if horario_min is not None else BLOQUE_AM_START_MIN
            pm_start = max(BLOQUE_PM_START_MIN, horario_min) if horario_min is not None else BLOQUE_PM_START_MIN
            vehs.append({"vehiculo_base": row["vehiculo_base"], "vuelta_slot": 1, "bloque": "AM", "capacidad": cap, "slot_start": am_start, "slot_end": BLOQUE_AM_END_MIN, "horario_min": horario_min})
            vehs.append({"vehiculo_base": row["vehiculo_base"], "vuelta_slot": 2, "bloque": "PM", "capacidad": cap, "slot_start": pm_start, "slot_end": BLOQUE_PM_END_MIN, "horario_min": horario_min})
        return pd.DataFrame(vehs)

    def armar_salida_desde_asignaciones(asignaciones, status, matrix_source="heuristica"):
        routes = []
        detail_rows = []
        for route_id, asign in enumerate(asignaciones, start=1):
            veh = asign["vehiculo"]
            stops = asign["stops"]
            if not stops:
                continue
            route_coords = [HUB_COORD]
            total_os = total_vol = total_kilo = 0
            out_stops = []
            for seq, item in enumerate(stops, start=1):
                p = item["p"]
                arr = item["eta_min"]
                total_os += float(p["os"]); total_vol += float(p["volumen"]); total_kilo += float(p["kilo_mayor"])
                stop = {
                    "escenario": escenario, "id_ruta_optimizada": route_id,
                    "vehiculo_base": veh["vehiculo_base"], "vuelta": int(veh["vuelta_slot"]), "bloque": veh.get("bloque", ""),
                    "secuencia": seq, "id_punto_opt": p["id_punto_opt"], "lat": float(p["lat"]), "lon": float(p["lon"]),
                    "eta": minutes_to_time(arr), "eta_min": arr, "tw_start": int(p["tw_start"]), "tw_end": int(p["tw_end"]),
                    "cumple_vh_estimado": bool(item.get("cumple_vh", True)),
                    "os": float(p["os"]), "volumen": float(p["volumen"]), "volumen_original": float(p.get("volumen_original", p["volumen"])), "kilo_mayor": float(p["kilo_mayor"]),
                    "m3_por_os_max": float(p.get("m3_por_os_max", 0)), "capmax_usado": float(p.get("capmax_usado", 0)), "volumen_ajustado": bool(p.get("volumen_ajustado", False)),
                    "ruta_real_original": p["ruta_original"], "vuelta_real_original": p["vuelta_original"]
                }
                out_stops.append(stop); detail_rows.append(stop); route_coords.append((float(p["lat"]), float(p["lon"])))
            route_coords.append(HUB_COORD)
            geom, ok_geom, km = get_route_geometry(tuple(route_coords), usar_osrm=usar_osrm_geometry)
            capacidad = float(veh["capacidad"])
            inicio = min([s["eta_min"] for s in stops]) if stops else np.nan
            fin = asign.get("fin_min", max([s["eta_min"] for s in stops]) if stops else inicio)
            routes.append({
                "escenario": escenario, "id_ruta_optimizada": route_id, "vehiculo_base": veh["vehiculo_base"], "vuelta": int(veh["vuelta_slot"]), "bloque": veh.get("bloque", ""),
                "capacidad": capacidad, "factor_ocupacion": total_vol / capacidad * 100 if capacidad else np.nan,
                "color": color_from_text(f"{escenario}-{route_id}"), "geometry": geom, "geometry_ok": ok_geom,
                "stops": out_stops, "total_os": total_os, "volumen": total_vol, "kilo_mayor": total_kilo,
                "paradas": len(out_stops), "km_estimado": km, "inicio_ruta": minutes_to_time(inicio), "fin_ruta": minutes_to_time(fin),
                "tiempo_ruta_min": max(0, fin - veh["slot_start"]) if pd.notna(fin) else np.nan,
                "activo": True
            })
        resumen = pd.DataFrame([{k:v for k,v in r.items() if k not in ["geometry", "stops", "color"]} for r in routes])
        return routes, pd.DataFrame(detail_rows), resumen, status

    def heuristica_am_pm():
        vehiculos = construir_vehiculos(flota)
        # Orden operacional: primero las ventanas que vencen antes. PM queda naturalmente después.
        pendientes = puntos.copy().sort_values(["tw_end", "tw_start", "datetime_gestion" if "datetime_gestion" in puntos.columns else "id_punto_opt"]).to_dict("records")
        asignaciones = []
        no_asignados = []

        def travel_min(a_lat, a_lon, b_lat, b_lon):
            return int(math.ceil(haversine_km(a_lat, a_lon, b_lat, b_lon) * 1.35 / 28 * 60))

        for _, veh in vehiculos.iterrows():
            if not pendientes:
                break
            cap_rest = float(veh["capacidad"])
            t = int(veh["slot_start"])
            lat, lon = HUB_COORD
            stops = []

            while pendientes:
                candidatos = []
                for i, p in enumerate(pendientes):
                    dem = float(p["volumen"])
                    if dem > cap_rest + 1e-9:
                        continue
                    viaje = travel_min(lat, lon, float(p["lat"]), float(p["lon"]))
                    eta = max(t + viaje, int(p["tw_start"]))
                    servicio = int(math.ceil(TIEMPO_SERVICIO_BASE + float(p["os"]) * TIEMPO_SERVICIO_OS))
                    retorno = travel_min(float(p["lat"]), float(p["lon"]), HUB_COORD[0], HUB_COORD[1])
                    fin_est = eta + servicio + retorno
                    cumple_vh = eta <= int(p["tw_end"])
                    cumple_bloque = fin_est <= int(veh["slot_end"]) and (fin_est - int(veh["slot_start"]) <= TIEMPO_MAX_RUTA_MIN)
                    if cumple_vh and cumple_bloque:
                        # Priorizamos menor vencimiento y cercanía.
                        candidatos.append((int(p["tw_end"]), viaje, i, eta, servicio, fin_est))
                if not candidatos:
                    break
                _, _, idx, eta, servicio, fin_est = min(candidatos)
                p = pendientes.pop(idx)
                stops.append({"p": p, "eta_min": eta, "cumple_vh": True})
                cap_rest -= float(p["volumen"])
                t = eta + servicio
                lat, lon = float(p["lat"]), float(p["lon"])

            if stops:
                fin = t + travel_min(lat, lon, HUB_COORD[0], HUB_COORD[1])
                asignaciones.append({"vehiculo": veh.to_dict(), "stops": stops, "fin_min": fin})

        # Si quedan puntos, no los dejamos invisibles. Los marcamos en rutas de excepción
        # para poder diagnosticar: normalmente son casos con ventana/capacidad imposible.
        if pendientes:
            max_cap = max(float(flota["capacidad"].max()), DEFAULT_CAPACIDAD)
            for j, p in enumerate(pendientes, start=1):
                bloque = "AM" if int(p["tw_end"]) <= HORA_CORTE_HUB_MIN else "PM"
                slot_start = BLOQUE_AM_START_MIN if bloque == "AM" else BLOQUE_PM_START_MIN
                slot_end = BLOQUE_AM_END_MIN if bloque == "AM" else BLOQUE_PM_END_MIN
                eta = max(slot_start, int(p["tw_start"]))
                veh = {"vehiculo_base": f"PENDIENTE-{j}", "vuelta_slot": 1 if bloque == "AM" else 2, "bloque": bloque, "capacidad": max(max_cap, float(p["volumen"])), "slot_start": slot_start, "slot_end": slot_end}
                asignaciones.append({"vehiculo": veh, "stops": [{"p": p, "eta_min": eta, "cumple_vh": eta <= int(p["tw_end"])}], "fin_min": eta + int(math.ceil(TIEMPO_SERVICIO_BASE + float(p["os"]) * TIEMPO_SERVICIO_OS))})
                no_asignados.append(p)

        status = {"status": "ok_heuristica" if not no_asignados else "ok_con_puntos_diagnostico", "matrix_source": "haversine_fallback", "modelo": "heuristica_bloques_am_pm", "puntos_diagnostico": len(no_asignados)}
        return armar_salida_desde_asignaciones(asignaciones, status, matrix_source="haversine_fallback")

    # Para días grandes, OR-Tools con ventanas + cientos de nodos suele no encontrar primera solución
    # dentro de un tiempo razonable. Vamos directo a la heurística para mantener el aplicativo usable.
    if len(puntos) > 180:
        return heuristica_am_pm()

    # OR-Tools para casos pequeños/medianos
    try:
        coords = [HUB_COORD] + list(zip(puntos["lat"], puntos["lon"]))
        matrix, matrix_source = get_vial_matrix(tuple(coords), usar_osrm=usar_osrm_matrix)
        vehiculos = construir_vehiculos(flota)
        n_veh = len(vehiculos)
        manager = pywrapcp.RoutingIndexManager(len(coords), n_veh, 0)
        routing = pywrapcp.RoutingModel(manager)
        service_times = [0] + [int(math.ceil(TIEMPO_SERVICIO_BASE + float(os)*TIEMPO_SERVICIO_OS)) for os in puntos["os"]]
        def time_cb(f, t):
            fi = manager.IndexToNode(f); ti = manager.IndexToNode(t)
            return int(matrix[fi][ti] + (service_times[fi] if fi != 0 else 0))
        transit = routing.RegisterTransitCallback(time_cb)
        routing.SetArcCostEvaluatorOfAllVehicles(transit)
        routing.AddDimension(transit, 30, 1440, False, "Time")
        time_dim = routing.GetDimensionOrDie("Time")
        windows = [(0,1439)] + list(zip(puntos["tw_start"].astype(int), puntos["tw_end"].astype(int)))
        for i,(s,e) in enumerate(windows):
            time_dim.CumulVar(manager.NodeToIndex(i)).SetRange(int(s), int(e))
        for v in range(n_veh):
            s, e = int(vehiculos.loc[v,"slot_start"]), int(vehiculos.loc[v,"slot_end"])
            time_dim.CumulVar(routing.Start(v)).SetRange(s, e)
            time_dim.CumulVar(routing.End(v)).SetRange(s, e)
            time_dim.SetSpanUpperBoundForVehicle(TIEMPO_MAX_RUTA_MIN, v)
        CAP_SCALE = 1000  # m3 -> litros aprox. para que OR-Tools trabaje con enteros sin perder decimales
        def demand_cb(idx):
            node = manager.IndexToNode(idx)
            return 0 if node == 0 else int(math.ceil(float(puntos.iloc[node-1]["volumen"]) * CAP_SCALE))
        demand = routing.RegisterUnaryTransitCallback(demand_cb)
        vehicle_caps = [int(max(1, math.floor(float(c) * CAP_SCALE))) for c in vehiculos["capacidad"]]
        routing.AddDimensionWithVehicleCapacity(demand, 0, vehicle_caps, True, "Capacity")
        routing.SetFixedCostOfAllVehicles(1000000)
        params = pywrapcp.DefaultRoutingSearchParameters()
        params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        params.time_limit.seconds = 20
        solution = routing.SolveWithParameters(params)
        if not solution:
            return heuristica_am_pm()

        asignaciones = []
        for v in range(n_veh):
            if not routing.IsVehicleUsed(solution, v):
                continue
            idx = routing.Start(v); stops=[]
            while not routing.IsEnd(idx):
                node = manager.IndexToNode(idx)
                if node != 0:
                    arr = solution.Value(time_dim.CumulVar(idx))
                    stops.append({"p": puntos.iloc[node-1].to_dict(), "eta_min": arr, "cumple_vh": True})
                idx = solution.Value(routing.NextVar(idx))
            end_min = solution.Value(time_dim.CumulVar(idx))
            if stops:
                asignaciones.append({"vehiculo": vehiculos.loc[v].to_dict(), "stops": stops, "fin_min": end_min})
        return armar_salida_desde_asignaciones(asignaciones, {"status":"ok", "matrix_source":matrix_source, "modelo":"ortools_bloques_am_pm"}, matrix_source)
    except Exception as e:
        routes, detail, resumen, meta = heuristica_am_pm()
        meta["error_ortools"] = str(e)[:200]

        return routes, detail, resumen, meta


def optimizar_ruta_preservando_flota(puntos, flota_baseline, usar_osrm_geometry=True):
    """
    v11 - Optimización de ruta estricta.

    Regla de negocio:
    - No reduce flota.
    - No consolida demanda entre rutas.
    - Cada reserva queda asociada a su ruta real original.
    - Si una ruta baseline no tiene demanda PU optimizable, queda visible como
      SIN_DEMANDA_OPTIMIZABLE, pero no desaparece del resumen.

    Este escenario representa re-secuenciar AM/PM dentro de la misma ruta,
    no ahorrar vehículos. El ahorro de vehículos queda exclusivamente para
    Optimización de capacidades.
    """
    scenario = "Optimización de ruta"
    flota = flota_baseline.copy().reset_index(drop=True)
    puntos = puntos.copy().reset_index(drop=True)
    if flota.empty:
        return [], pd.DataFrame(), pd.DataFrame(), {"status": "sin_flota", "modelo": "ruta_preservada_v11"}

    def travel_min(a_lat, a_lon, b_lat, b_lon):
        return int(math.ceil(haversine_km(a_lat, a_lon, b_lat, b_lon) * 1.35 / 28 * 60))

    def ordenar_bloque(df, start_lat=HUB_LAT, start_lon=HUB_LON):
        """Ordena primero por vencimiento VH y usa cercanía como desempate simple."""
        pendientes = df.to_dict("records")
        orden = []
        lat, lon = start_lat, start_lon
        while pendientes:
            candidatos = []
            for i, p in enumerate(pendientes):
                dist = travel_min(lat, lon, float(p["lat"]), float(p["lon"]))
                candidatos.append((int(p.get("tw_end", 1439)), int(p.get("tw_start", 0)), dist, i))
            _, _, _, idx = min(candidatos)
            p = pendientes.pop(idx)
            orden.append(p)
            lat, lon = float(p["lat"]), float(p["lon"])
        return orden

    routes = []
    detail_rows = []
    resumen_rows = []
    route_id = 1

    for _, veh_row in flota.iterrows():
        vehiculo = str(veh_row["vehiculo_base"])
        capacidad = float(max(0.001, veh_row.get("capacidad", DEFAULT_CAPACIDAD)))
        pveh = puntos[puntos["ruta_original"].astype(str) == vehiculo].copy() if not puntos.empty else pd.DataFrame()

        if pveh.empty:
            resumen_rows.append({
                "escenario": scenario, "id_ruta_optimizada": route_id,
                "vehiculo_base": vehiculo, "vuelta": 0, "bloque": "SIN_DEMANDA_OPTIMIZABLE",
                "capacidad": capacidad, "factor_ocupacion": 0.0, "geometry_ok": False,
                "total_os": 0.0, "volumen": 0.0, "kilo_mayor": 0.0, "paradas": 0,
                "km_estimado": 0.0, "inicio_ruta": "-", "fin_ruta": "-", "tiempo_ruta_min": 0.0,
                "activo": False
            })
            route_id += 1
            continue

        # Separación AM/PM flexible por ventana. Si vence antes del corte, AM; si no, PM.
        horario_min = veh_row.get("horario_min", np.nan)
        horario_min = int(horario_min) if pd.notna(horario_min) else None
        am_start = max(BLOQUE_AM_START_MIN, horario_min) if horario_min is not None else BLOQUE_AM_START_MIN
        pm_start = max(BLOQUE_PM_START_MIN, horario_min) if horario_min is not None else BLOQUE_PM_START_MIN
        bloques = [
            ("AM", 1, am_start, BLOQUE_AM_END_MIN, pveh[pveh["tw_end"] <= HORA_CORTE_HUB_MIN].copy()),
            ("PM", 2, pm_start, BLOQUE_PM_END_MIN, pveh[pveh["tw_end"] > HORA_CORTE_HUB_MIN].copy()),
        ]

        for bloque, vuelta, slot_start, slot_end, g in bloques:
            if g.empty:
                continue
            orden = ordenar_bloque(g)
            lat, lon = HUB_COORD
            t = int(slot_start)
            route_coords = [HUB_COORD]
            stops = []
            total_os = total_vol = total_kilo = 0.0
            cumple_all = True

            for seq, p in enumerate(orden, start=1):
                viaje = travel_min(lat, lon, float(p["lat"]), float(p["lon"]))
                eta = max(t + viaje, int(p.get("tw_start", 0)))
                servicio = int(math.ceil(TIEMPO_SERVICIO_BASE + float(p.get("os", 0)) * TIEMPO_SERVICIO_OS))
                cumple = bool(eta <= int(p.get("tw_end", 1439)))
                cumple_all = cumple_all and cumple
                total_os += float(p.get("os", 0)); total_vol += float(p.get("volumen", 0)); total_kilo += float(p.get("kilo_mayor", 0))

                stop = {
                    "escenario": scenario, "id_ruta_optimizada": route_id, "vehiculo_base": vehiculo,
                    "vuelta": int(vuelta), "bloque": bloque, "secuencia": seq,
                    "id_punto_opt": p.get("id_punto_opt", ""), "lat": float(p["lat"]), "lon": float(p["lon"]),
                    "eta": minutes_to_time(eta), "eta_min": eta, "tw_start": int(p.get("tw_start", 0)), "tw_end": int(p.get("tw_end", 1439)),
                    "cumple_vh_estimado": cumple, "os": float(p.get("os", 0)), "volumen": float(p.get("volumen", 0)),
                    "volumen_original": float(p.get("volumen_original", p.get("volumen", 0))), "kilo_mayor": float(p.get("kilo_mayor", 0)),
                    "m3_por_os_max": float(p.get("m3_por_os_max", 0)), "capmax_usado": float(p.get("capmax_usado", 0)), "volumen_ajustado": bool(p.get("volumen_ajustado", False)),
                    "ruta_real_original": p.get("ruta_original", vehiculo), "vuelta_real_original": p.get("vuelta_original", "")
                }
                stops.append(stop); detail_rows.append(stop)
                route_coords.append((float(p["lat"]), float(p["lon"])))
                t = eta + servicio
                lat, lon = float(p["lat"]), float(p["lon"])

            retorno = travel_min(lat, lon, HUB_COORD[0], HUB_COORD[1])
            fin = t + retorno
            route_coords.append(HUB_COORD)
            geom, ok_geom, km = get_route_geometry(tuple(route_coords), usar_osrm=usar_osrm_geometry)
            routes.append({
                "escenario": scenario, "id_ruta_optimizada": route_id, "vehiculo_base": vehiculo,
                "vuelta": int(vuelta), "bloque": bloque, "capacidad": capacidad,
                "factor_ocupacion": total_vol / capacidad * 100 if capacidad else np.nan,
                "color": color_from_text(f"{scenario}-{vehiculo}-{vuelta}"), "geometry": geom, "geometry_ok": ok_geom,
                "stops": stops, "total_os": total_os, "volumen": total_vol, "kilo_mayor": total_kilo,
                "paradas": len(stops), "km_estimado": km, "inicio_ruta": minutes_to_time(slot_start),
                "fin_ruta": minutes_to_time(fin), "tiempo_ruta_min": max(0, fin - slot_start), "activo": True,
                "cumple_vh_ruta": cumple_all
            })
            resumen_rows.append({k:v for k,v in routes[-1].items() if k not in ["geometry", "stops", "color"]})
            route_id += 1

    resumen = pd.DataFrame(resumen_rows)
    detail = pd.DataFrame(detail_rows)
    meta = {
        "status": "ok_ruta_preservada",
        "matrix_source": "haversine_fallback_para_secuencia_y_osrm_para_mapa",
        "modelo": "ruta_preservada_v11",
        "rutas_flota_baseline": int(flota["vehiculo_base"].nunique()),
        "rutas_con_demanda_optimizable": int(puntos["ruta_original"].nunique()) if not puntos.empty else 0
    }
    return routes, detail, resumen, meta

# ============================================================
# MÉTRICAS Y MAPAS
# ============================================================
def metricas_baseline(baseline, dicruta, cols):
    col_q_os, col_km, col_reserva = cols["col_q_os"], cols["col_km"], cols["col_reserva"]
    cap_map = dicruta.set_index("ruta")["capacidad"].to_dict()
    d = baseline.copy()
    d["capacidad_ruta"] = d["ruta"].map(cap_map).fillna(DEFAULT_CAPACIDAD)
    if col_km:
        q_os_base = pd.to_numeric(d[col_q_os], errors="coerce").fillna(1) if col_q_os else pd.Series(1, index=d.index)
        ajuste_vol = ajustar_volumen_m3(d[col_km], q_os_base, capmax_por_os=d["ruta"].map(dicruta.set_index("ruta")["capmax"].to_dict()) if "capmax" in dicruta.columns else None, umbral_m3_por_os=3.0, m3_estandar_por_os=0.33)
        d["volumen_original_m3"] = ajuste_vol["volumen_original_m3"]
        d["volumen_calc"] = ajuste_vol["volumen_m3"]
        d["m3_por_os"] = ajuste_vol["m3_por_os"]
        d["volumen_ajustado"] = ajuste_vol["volumen_ajustado"]
    else:
        d["volumen_original_m3"] = 0
        d["volumen_calc"] = 0
        d["m3_por_os"] = 0
        d["volumen_ajustado"] = False
    rutas = d["ruta"].nunique()
    vueltas = d.dropna(subset=["vuelta"]).drop_duplicates(["ruta","vuelta"]).shape[0]
    vol = d["volumen_calc"].sum()
    tmin = (d.groupby("ruta")["datetime_gestion"].max() - d.groupby("ruta")["datetime_gestion"].min()).dt.total_seconds().fillna(0).sum()/60
    total_os = pd.to_numeric(d[col_q_os], errors="coerce").fillna(0).sum() if col_q_os else len(d)
    reservas = d[col_reserva].nunique() if col_reserva else len(d)

    # Ocupación v8: la métrica macro es el promedio simple de la ocupación de cada vuelta ejecutada.
    if d["vuelta"].notna().any():
        occ_base = d.groupby(["ruta","vuelta"], dropna=False).agg(
            volumen_vuelta=("volumen_calc", "sum"),
            capacidad=("capacidad_ruta", "first")
        ).reset_index()
    else:
        occ_base = d.groupby(["ruta"], dropna=False).agg(
            volumen_vuelta=("volumen_calc", "sum"),
            capacidad=("capacidad_ruta", "first")
        ).reset_index()
    occ_base["factor_ocupacion_vuelta"] = np.where(
        occ_base["capacidad"] > 0,
        occ_base["volumen_vuelta"] / occ_base["capacidad"] * 100,
        np.nan
    )
    factor_macro = occ_base["factor_ocupacion_vuelta"].mean() if not occ_base.empty else np.nan
    capacidad_total = occ_base["capacidad"].sum() if not occ_base.empty else np.nan

    return {
        "escenario":"Baseline", "cantidad_rutas":rutas, "vueltas":vueltas, "total_os":total_os, "reservas":reservas,
        "volumen":vol, "cumplimiento_vh": d["cumple_ventana"].mean()*100 if d["cumple_ventana"].notna().any() else np.nan,
        "excepciones": d["es_excepcion"].mean()*100 if len(d) else np.nan,
        "productividad": total_os/rutas if rutas else np.nan,
        "factor_ocupacion": factor_macro,
        "ocupacion_vehiculo_dia": factor_macro,
        "capacidad_total": capacidad_total,
        "tiempo_ruta": tmin/rutas if rutas else np.nan,
    }

def metricas_opt(resumen, puntos, escenario, rutas_base_objetivo=None):
    """
    Métricas de escenarios optimizados.

    Reglas corregidas v7:
    - Optimización de ruta mantiene la misma flota física del baseline. Por eso
      cantidad_rutas y productividad se calculan con rutas_base_objetivo.
    - Rutas PENDIENTE-* son diagnóstico/no factibles: no cuentan como rutas
      optimizadas, vueltas, capacidad ni ocupación.
    - factor_ocupacion se expresa como porcentaje real: volumen_m3 / capacidad_m3 * 100.
    """
    total_os = puntos["os"].sum() if not puntos.empty else 0
    reservas = puntos["id_punto_opt"].nunique() if not puntos.empty else 0
    volumen = puntos["volumen"].sum() if not puntos.empty else 0

    if resumen.empty:
        rutas_calc = rutas_base_objetivo if rutas_base_objetivo is not None else 0
        return {"escenario":escenario, "cantidad_rutas":rutas_calc, "vueltas":0, "total_os":total_os,
                "reservas":reservas, "volumen":volumen, "capacidad_total":np.nan, "cumplimiento_vh":np.nan, "excepciones":np.nan,
                "productividad": total_os/rutas_calc if rutas_calc else np.nan, "factor_ocupacion":np.nan, "tiempo_ruta":np.nan}

    resumen_ok = resumen[~resumen["vehiculo_base"].astype(str).str.startswith("PENDIENTE")].copy()
    resumen_diag = resumen[resumen["vehiculo_base"].astype(str).str.startswith("PENDIENTE")].copy()

    if resumen_ok.empty:
        rutas_calc = rutas_base_objetivo if rutas_base_objetivo is not None else 0
        return {"escenario":escenario, "cantidad_rutas":rutas_calc, "vueltas":0, "total_os":total_os,
                "reservas":reservas, "volumen":volumen, "capacidad_total":np.nan, "cumplimiento_vh":0 if not resumen_diag.empty else np.nan, "excepciones":np.nan,
                "productividad": total_os/rutas_calc if rutas_calc else np.nan, "factor_ocupacion":np.nan, "tiempo_ruta":np.nan}

    if "activo" not in resumen_ok.columns:
        resumen_ok["activo"] = pd.to_numeric(resumen_ok.get("paradas", 0), errors="coerce").fillna(0) > 0
    else:
        resumen_ok["activo"] = resumen_ok["activo"].fillna(True).astype(bool)

    resumen_activo = resumen_ok[resumen_ok["activo"]].copy()
    rutas_usadas = resumen_activo["vehiculo_base"].nunique()
    rutas_fisicas = int(rutas_base_objetivo) if rutas_base_objetivo is not None else int(rutas_usadas)

    # v10: ocupación macro = promedio de ocupación vehículo-día.
    # En Optimización de ruta se incluyen vehículos sin carga con ocupación 0,
    # porque la flota se mantiene. En Optimización de capacidades solo quedan
    # vehículos efectivamente activados.
    occ_veh_por_dia = resumen_ok.groupby("vehiculo_base")["factor_ocupacion"].mean() if "factor_ocupacion" in resumen_ok.columns else pd.Series(dtype=float)
    occ_veh_dia = occ_veh_por_dia.mean() if len(occ_veh_por_dia) else np.nan
    factor_macro = occ_veh_dia
    capacidad_total = resumen_activo["capacidad"].sum() if "capacidad" in resumen_activo.columns and not resumen_activo.empty else np.nan

    return {
        "escenario":escenario,
        "cantidad_rutas":rutas_fisicas,
        "vueltas":len(resumen_activo),
        "total_os":total_os,
        "reservas":reservas,
        "volumen":volumen,
        "capacidad_total":capacidad_total,
        "cumplimiento_vh":100.0 if resumen_diag.empty else np.nan,
        "excepciones":np.nan,
        "productividad": total_os/rutas_fisicas if rutas_fisicas else np.nan,
        "factor_ocupacion": factor_macro,
        "ocupacion_vehiculo_dia": occ_veh_dia,
        "tiempo_ruta": resumen_activo["tiempo_ruta_min"].mean() if not resumen_activo.empty else 0
    }

def resumen_por_ruta_baseline(baseline, dicruta, cols):
    col_reserva, col_q_os, col_km = cols["col_reserva"], cols["col_q_os"], cols["col_km"]
    cap_map = dicruta.set_index("ruta")["capacidad"].to_dict()
    d = baseline.copy()
    if col_km:
        q_os_base = pd.to_numeric(d[col_q_os], errors="coerce").fillna(1) if col_q_os else pd.Series(1, index=d.index)
        ajuste_vol = ajustar_volumen_m3(d[col_km], q_os_base, capmax_por_os=d["ruta"].map(dicruta.set_index("ruta")["capmax"].to_dict()) if "capmax" in dicruta.columns else None, umbral_m3_por_os=3.0, m3_estandar_por_os=0.33)
        d["volumen_original_m3"] = ajuste_vol["volumen_original_m3"]
        d["volumen"] = ajuste_vol["volumen_m3"]
        d["m3_por_os"] = ajuste_vol["m3_por_os"]
        d["volumen_ajustado"] = ajuste_vol["volumen_ajustado"]
    else:
        d["volumen_original_m3"] = 0
        d["volumen"] = 0
        d["m3_por_os"] = 0
        d["volumen_ajustado"] = False
    d["capacidad_ruta"] = d["ruta"].map(cap_map).fillna(DEFAULT_CAPACIDAD)
    occ_vuelta_tmp = d.groupby(["ruta","vuelta"], dropna=False).agg(
        volumen_vuelta=("volumen", "sum"),
        capacidad=("capacidad_ruta", "first")
    ).reset_index()
    occ_vuelta_tmp["factor_ocupacion_vuelta"] = np.where(
        occ_vuelta_tmp["capacidad"] > 0,
        occ_vuelta_tmp["volumen_vuelta"] / occ_vuelta_tmp["capacidad"] * 100,
        np.nan
    )
    occ_ruta_map = occ_vuelta_tmp.groupby("ruta")["factor_ocupacion_vuelta"].mean().to_dict()
    out = d.groupby(["fecha_operacion","ruta"], dropna=False).agg(
        vueltas=("vuelta", lambda s: s.dropna().nunique()), reservas=(col_reserva,"nunique"), total_os=(col_q_os,"sum"),
        volumen=("volumen","sum"), volumen_original_m3=("volumen_original_m3", "sum"), reservas_ajustadas=("volumen_ajustado", "sum"),
        cumplimiento_vh=("cumple_ventana","mean"), excepciones=("es_excepcion","mean"),
        primer_pu=("datetime_gestion","min"), ultimo_pu=("datetime_gestion","max")
    ).reset_index()
    out["capacidad"] = out["ruta"].map(cap_map).fillna(DEFAULT_CAPACIDAD)
    out["factor_ocupacion"] = out["ruta"].map(occ_ruta_map)
    out["productividad"] = out["total_os"] / 1
    out["cumplimiento_vh"] *= 100; out["excepciones"] *= 100
    out["escenario"] = "Baseline"
    return out

def resumen_por_vuelta_baseline(baseline, dicruta, cols):
    col_reserva, col_q_os, col_km = cols["col_reserva"], cols["col_q_os"], cols["col_km"]
    cap_map = dicruta.set_index("ruta")["capacidad"].to_dict()
    d = baseline.copy()
    if col_km:
        q_os_base = pd.to_numeric(d[col_q_os], errors="coerce").fillna(1) if col_q_os else pd.Series(1, index=d.index)
        ajuste_vol = ajustar_volumen_m3(d[col_km], q_os_base, capmax_por_os=d["ruta"].map(dicruta.set_index("ruta")["capmax"].to_dict()) if "capmax" in dicruta.columns else None, umbral_m3_por_os=3.0, m3_estandar_por_os=0.33)
        d["volumen_original_m3"] = ajuste_vol["volumen_original_m3"]
        d["volumen"] = ajuste_vol["volumen_m3"]
        d["m3_por_os"] = ajuste_vol["m3_por_os"]
        d["volumen_ajustado"] = ajuste_vol["volumen_ajustado"]
    else:
        d["volumen_original_m3"] = 0
        d["volumen"] = 0
        d["m3_por_os"] = 0
        d["volumen_ajustado"] = False
    out = d.groupby(["fecha_operacion","ruta","vuelta"], dropna=False).agg(
        reservas=(col_reserva,"nunique"), total_os=(col_q_os,"sum"), volumen=("volumen","sum"),
        volumen_original_m3=("volumen_original_m3", "sum"), reservas_ajustadas=("volumen_ajustado", "sum"),
        cumplimiento_vh=("cumple_ventana","mean"), excepciones=("es_excepcion","mean"),
        primer_pu=("datetime_gestion","min"), ultimo_pu=("datetime_gestion","max"), cierre_vuelta=("datetime_cierre_vuelta","max")
    ).reset_index()
    out["capacidad"] = out["ruta"].map(cap_map).fillna(DEFAULT_CAPACIDAD)
    out["factor_ocupacion"] = out["volumen"] / out["capacidad"] * 100
    out["cumplimiento_vh"] *= 100; out["excepciones"] *= 100
    out["escenario"] = "Baseline"
    return out

def preparar_mapa_baseline(baseline, cols):
    lat_col, lon_col = cols["lat_col"], cols["lon_col"]
    if not lat_col or not lon_col: return pd.DataFrame(), pd.DataFrame()
    mapa = baseline.dropna(subset=[lat_col, lon_col]).sort_values(["ruta","vuelta","datetime_gestion"]).copy()
    if mapa.empty: return pd.DataFrame(), pd.DataFrame()
    mapa["orden"] = mapa.groupby(["ruta","vuelta"]).cumcount()+1
    mapa["orden_txt"] = mapa["orden"].astype(str)
    mapa["color"] = mapa.apply(lambda r: color_from_text(f"BL-{r['ruta']}-{r['vuelta']}"), axis=1)
    paths=[]
    for (ruta,vuelta), g in mapa.groupby(["ruta","vuelta"], dropna=False):
        coords = [HUB_COORD] + [(float(r[lat_col]), float(r[lon_col])) for _,r in g.iterrows()] + [HUB_COORD]
        geom, ok, km = get_route_geometry(tuple(coords), usar_osrm=True)
        paths.append({"ruta":ruta, "vuelta":str(vuelta), "id":f"{ruta}-{vuelta}", "path":[[lon,lat] for lat,lon in geom], "color":color_from_text(f"BL-{ruta}-{vuelta}"), "km_estimado":km})
    mapa = mapa.rename(columns={lat_col:"lat", lon_col:"lon"})
    return mapa, pd.DataFrame(paths)

def preparar_mapa_opt(routes):
    stops=[]; paths=[]
    for r in routes:
        veh = str(r.get("vehiculo_base", ""))
        vuelta = str(r.get("vuelta", ""))
        paths.append({
            "id": f"{veh}-V{vuelta}",
            "vehiculo_base": veh,
            "ruta": veh,
            "vuelta": vuelta,
            "path": [[lon,lat] for lat,lon in r["geometry"]],
            "color": r["color"]
        })
        for s in r["stops"]:
            row=s.copy(); row["color"]=r["color"]; row["orden_txt"]=str(row["secuencia"]); stops.append(row)
    return pd.DataFrame(stops), pd.DataFrame(paths)

def render_map(stops, paths, title, key_prefix="mapa"):
    st.markdown(f"#### {title}")
    if stops.empty or paths.empty:
        st.warning("No hay puntos para mostrar."); return

    # Filtro simple para presentar una sola ruta/vehículo sin recalcular el optimizador.
    ruta_col = "vehiculo_base" if "vehiculo_base" in stops.columns else "ruta"
    opciones = sorted([str(x) for x in stops[ruta_col].dropna().unique()])
    ver_todas = "Todas"
    seleccion = st.selectbox("Ver ruta / vehículo", [ver_todas] + opciones, key=f"{key_prefix}_ruta_visible")
    if seleccion != ver_todas:
        stops = stops[stops[ruta_col].astype(str) == seleccion].copy()
        if "vehiculo_base" in paths.columns:
            paths = paths[paths["vehiculo_base"].astype(str) == seleccion].copy()
        elif "ruta" in paths.columns:
            paths = paths[paths["ruta"].astype(str) == seleccion].copy()
        else:
            paths = paths[paths["id"].astype(str).str.startswith(seleccion)].copy()

    if stops.empty or paths.empty:
        st.warning("No hay puntos para la ruta seleccionada."); return

    path_layer = pdk.Layer("PathLayer", data=paths, get_path="path", get_width=8, width_min_pixels=4, get_color="color", pickable=True)
    point_layer = pdk.Layer("ScatterplotLayer", data=stops, get_position="[lon, lat]", get_radius=105, get_fill_color="color", get_line_color=[255,255,255], line_width_min_pixels=3, pickable=True)
    text_layer = pdk.Layer("TextLayer", data=stops, get_position="[lon, lat]", get_text="orden_txt", get_size=14, get_color=[255,255,255,255], get_text_anchor="'middle'", get_alignment_baseline="'center'")
    hub = pd.DataFrame([{"lat":HUB_LAT,"lon":HUB_LON,"color":[255,170,0,245],"nombre":"HUB1"}])
    hub_layer = pdk.Layer("ScatterplotLayer", data=hub, get_position="[lon, lat]", get_radius=220, get_fill_color="color", get_line_color=[255,255,255], line_width_min_pixels=3, pickable=True)
    view = pdk.ViewState(latitude=stops["lat"].mean(), longitude=stops["lon"].mean(), zoom=10, pitch=0)
    tooltip = {"html":"<b>ID:</b> {id_punto_opt}<br/><b>Ruta:</b> {ruta}<br/><b>Vehículo:</b> {vehiculo_base}<br/><b>Vuelta:</b> {vuelta}<br/><b>Orden:</b> {orden}<br/><b>ETA:</b> {eta}<br/><b>OS:</b> {os}<br/><b>Volumen m³:</b> {volumen}", "style":{"backgroundColor":"white","color":"black"}}
    st.pydeck_chart(pdk.Deck(map_style="light", layers=[path_layer, point_layer, text_layer, hub_layer], initial_view_state=view, tooltip=tooltip), use_container_width=True)

# ============================================================
# SIDEBAR / INPUTS
# ============================================================
with st.sidebar:
    st.header("Carga")
    archivo_peu = st.file_uploader("Archivo gestión PU", type=["xlsx"], key="peu")
    archivo_accesos = st.file_uploader("Archivo accesos / ingreso Hub1", type=["xlsx"], key="accesos")
    archivo_dicruta = st.file_uploader("Diccionario de rutas/capacidad (opcional)", type=["xlsx"], key="dicruta")
    st.caption("Si no cargas dicruta, el aplicativo intentará leer un archivo fijo llamado dicruta.xlsx junto al .py.")

if archivo_peu is None or archivo_accesos is None:
    st.info("Carga los archivos de gestión PU y accesos para iniciar. El optimizador se ejecutará automáticamente con los parámetros bloqueados.")
    st.stop()

try:
    peu_raw = read_excel_any(archivo_peu)
    accesos_raw = read_excel_any(archivo_accesos)
    if archivo_dicruta is not None:
        dicruta = cargar_dicruta(archivo_dicruta)
    elif DICRUTA_DEFAULT_PATH.exists():
        dicruta = cargar_dicruta(str(DICRUTA_DEFAULT_PATH))
    else:
        st.error("No encontré dicruta.xlsx. Cárgalo en la barra lateral o déjalo fijo junto al aplicativo.")
        st.stop()
except Exception as e:
    st.error(f"Error cargando archivos: {e}")
    st.stop()

peu, accesos, cols, faltantes = preparar_data(peu_raw, accesos_raw)
if faltantes:
    st.error("Faltan columnas necesarias: " + ", ".join(faltantes)); st.stop()

clases_disponibles = sorted(peu["clase_ruta"].dropna().unique())
tipos_disponibles = sorted(peu["tipo_ruta"].dropna().unique())
# Por defecto se consideran todas las clases de ruta disponibles.
default_clases = clases_disponibles
comunas_disponibles = sorted(peu["comuna"].dropna().unique())
default_comuna = "Todas"


def completar_resumen_ruta_con_flota(resumen, flota_baseline, escenario="Optimización de ruta"):
    """
    v10: En Optimización de ruta se mantiene la flota física del baseline.
    El resumen exportado debe mostrar también los vehículos sin carga, para que
    el conteo del reporte calce con la métrica de Streamlit y para que la
    ocupación promedio no excluya rutas vacías.
    """
    resumen = resumen.copy() if resumen is not None else pd.DataFrame()
    if flota_baseline is None or flota_baseline.empty:
        return resumen

    usados = set(resumen["vehiculo_base"].astype(str)) if (not resumen.empty and "vehiculo_base" in resumen.columns) else set()
    rows = []
    next_id = int(pd.to_numeric(resumen.get("id_ruta_optimizada", pd.Series(dtype=float)), errors="coerce").max() or 0) + 1
    for _, r in flota_baseline.iterrows():
        veh = str(r["vehiculo_base"])
        if veh in usados:
            continue
        rows.append({
            "escenario": escenario,
            "id_ruta_optimizada": next_id,
            "vehiculo_base": veh,
            "vuelta": 0,
            "bloque": "SIN_ASIGNACION",
            "capacidad": float(r.get("capacidad", DEFAULT_CAPACIDAD)),
            "factor_ocupacion": 0.0,
            "geometry_ok": False,
            "total_os": 0.0,
            "volumen": 0.0,
            "kilo_mayor": 0.0,
            "paradas": 0,
            "km_estimado": 0.0,
            "inicio_ruta": "-",
            "fin_ruta": "-",
            "tiempo_ruta_min": 0.0,
            "activo": False
        })
        next_id += 1
    if rows:
        resumen = pd.concat([resumen, pd.DataFrame(rows)], ignore_index=True, sort=False)
    if "activo" not in resumen.columns:
        resumen["activo"] = resumen.get("paradas", 0).fillna(0).astype(float) > 0
    return resumen

# ============================================================
# EJECUCIÓN AUTOMÁTICA DE ESCENARIOS
# ============================================================
def ejecutar_escenarios_dia(fecha, clases_sel, tipos_sel, comuna_sel=None):
    base = peu[(peu["fecha_operacion"] == fecha) & peu["clase_ruta"].isin(clases_sel) & peu["tipo_ruta"].isin(tipos_sel)].copy()
    if comuna_sel and comuna_sel != "Todas":
        base = base[base["comuna"].eq(comuna_sel)].copy()
    acc = accesos[accesos["solo_fch"] == fecha].sort_values(["ruta","datetime_ingreso"]).copy()
    baseline = asignar_vueltas_por_cierre(base, acc)
    flota_bl = construir_flotas_baseline(baseline, dicruta)
    # Para validar capacidad de puntos usamos SOLO la capacidad máxima de la flota usada en el baseline del día.
    # No se permite resolver con capacidades que no existieron en la operación filtrada.
    max_capacidad_disponible = float(flota_bl["capacidad"].max()) if not flota_bl.empty else DEFAULT_CAPACIDAD
    puntos = preparar_puntos(baseline, cols, max_capacidad=max_capacidad_disponible, dicruta=dicruta)
    flota_cap = construir_flotas_capacidades(flota_bl, puntos["volumen"].sum() if not puntos.empty else 0)
    routes_ruta, detail_ruta, resumen_ruta, meta_ruta = optimizar_ruta_preservando_flota(puntos, flota_bl, True)
    routes_cap, detail_cap, resumen_cap, meta_cap = optimizar_cached(puntos, flota_cap, "Optimización de capacidades", True, True)
    resumen_ruta = completar_resumen_ruta_con_flota(resumen_ruta, flota_bl, "Optimización de ruta")
    met_base = metricas_baseline(baseline, dicruta, cols)
    mets = pd.DataFrame([
        met_base,
        metricas_opt(resumen_ruta, puntos, "Optimización de ruta", rutas_base_objetivo=met_base["cantidad_rutas"]),
        metricas_opt(resumen_cap, puntos, "Optimización de capacidades")
    ])
    # Para la vista ejecutiva: los escenarios optimizados representan asignaciones que respetan ventana horaria.
    mets.loc[mets["escenario"].isin(["Optimización de ruta", "Optimización de capacidades"]), "cumplimiento_vh"] = 100.0
    # v9: no se fuerzan OS/reservas/volumen entre escenarios.
    # Baseline muestra todo el contexto ejecutado; escenarios optimizados muestran solo demanda PU optimizable
    # (eventos PU con coordenadas). Esto evita esconder excepciones o registros no optimizables.
    mets.insert(0, "fecha", fecha)
    mets.insert(1, "comuna", comuna_sel if comuna_sel else "Todas")
    escala_usada = float(puntos["factor_escala_capacidad"].iloc[0]) if not puntos.empty and "factor_escala_capacidad" in puntos.columns else 1.0
    return dict(baseline=baseline, puntos=puntos, metrics=mets, routes_ruta=routes_ruta, detail_ruta=detail_ruta, resumen_ruta_opt=resumen_ruta, meta_ruta=meta_ruta,
                routes_cap=routes_cap, detail_cap=detail_cap, resumen_cap_opt=resumen_cap, meta_cap=meta_cap, escala_capacidad=escala_usada)

# ============================================================
# FILTROS GENERALES
# ============================================================
st.markdown('<div class="section-title">Filtros generales de ejecución</div>', unsafe_allow_html=True)
st.caption("Estos filtros aplican a Análisis diario, Datos macro, Análisis por ruta y Exportar.")
fg1, fg2, fg3 = st.columns(3)
with fg1:
    filtro_clases = excel_filter("Clase ruta", clases_disponibles, "global_clase", default_clases)
with fg2:
    filtro_tipos = excel_filter("Tipo ruta", tipos_disponibles, "global_tipo", tipos_disponibles)
with fg3:
    filtro_comuna = excel_single_filter("Comuna", ["Todas"] + comunas_disponibles, "global_comuna", default_comuna)

# ============================================================
# TABS
# ============================================================
tab_analisis, tab_macro, tab_rutas, tab_exportar = st.tabs(["📈 Análisis diario", "📌 Datos macro diarios", "🗺️ Análisis por ruta", "⬇️ Exportar"])

with tab_analisis:
    st.markdown('<div class="section-title">Análisis diario comparativo</div>', unsafe_allow_html=True)
    st.caption("Selecciona una variable para revisar la evolución diaria entre escenarios. El contexto operacional se mantiene como referencia del día.")
    clases_sel = filtro_clases
    tipos_sel = filtro_tipos
    comuna_sel = filtro_comuna
    st.caption(f"Filtro aplicado · Comuna: {comuna_sel}")
    indicadores = ["cumplimiento_vh", "cantidad_rutas", "factor_ocupacion", "productividad", "tiempo_ruta"]
    nombres = {"cumplimiento_vh":"% Cumplimiento VH", "cantidad_rutas":"Cantidad de rutas", "factor_ocupacion":"% Ocupación", "productividad":"Productividad", "tiempo_ruta":"Tiempo en ruta"}
    indicador_sel = excel_single_filter("Variable", [nombres[i] for i in indicadores], "ana_var", nombres["productividad"])
    inv = {v:k for k,v in nombres.items()}; var = inv[indicador_sel]

    peu_fechas = peu[(peu["clase_ruta"].isin(clases_sel)) & (peu["tipo_ruta"].isin(tipos_sel))].copy()
    if comuna_sel and comuna_sel != "Todas":
        peu_fechas = peu_fechas[peu_fechas["comuna"].eq(comuna_sel)]
    fechas = sorted(peu_fechas["fecha_operacion"].dropna().unique())
    rows=[]
    prog = st.progress(0, text="Ejecutando escenarios diarios automáticamente...") if fechas else None
    for i, f in enumerate(fechas):
        res = ejecutar_escenarios_dia(f, clases_sel, tipos_sel, comuna_sel)
        rows.append(res["metrics"])
        if prog: prog.progress((i+1)/len(fechas), text=f"Escenarios calculados: {i+1}/{len(fechas)}")
    if prog: prog.empty()
    diario = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    st.session_state["diario_comparativo"] = diario

    if diario.empty:
        st.warning("No hay datos para graficar.")
    else:
        chart_df = diario[["fecha","escenario",var]].rename(columns={var:"valor"})
        chart_df["fecha"] = pd.to_datetime(chart_df["fecha"])
        chart = alt.Chart(chart_df).mark_line(point=True, strokeWidth=3).encode(
            x=alt.X("fecha:T", title="Día operacional"), y=alt.Y("valor:Q", title=indicador_sel),
            color=alt.Color("escenario:N", title="Escenario"), tooltip=["fecha:T", "escenario:N", alt.Tooltip("valor:Q", format=".2f")]
        ).properties(height=320).interactive()
        st.altair_chart(chart, use_container_width=True)

        st.markdown("#### Contexto operacional del día")
        cols_contexto = ["fecha", "total_os", "reservas", "volumen"]
        if "capacidad_total" in diario.columns:
            cols_contexto.append("capacidad_total")
        contexto = diario[diario["escenario"].eq("Baseline")][cols_contexto].copy()
        contexto = contexto.melt(
            id_vars=["fecha"],
            value_vars=[c for c in cols_contexto if c != "fecha"],
            var_name="indicador",
            value_name="valor"
        )
        contexto["fecha"] = pd.to_datetime(contexto["fecha"])
        contexto["indicador"] = contexto["indicador"].map({
            "total_os": "Total OS",
            "reservas": "Reservas",
            "volumen": "Volumen operacional (m³)",
            "capacidad_total": "Capacidad utilizada/referencial (m³)"
        })

        charts_contexto = []
        orden_indicadores = ["Reservas", "Total OS", "Volumen operacional (m³)", "Capacidad utilizada/referencial (m³)"]
        for indicador in orden_indicadores:
            df_ind = contexto[contexto["indicador"].eq(indicador)].copy()
            if df_ind.empty:
                continue
            charts_contexto.append(
                alt.Chart(df_ind).mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4).encode(
                    x=alt.X("fecha:T", title="Día operacional"),
                    y=alt.Y("valor:Q", title=indicador),
                    tooltip=["fecha:T", "indicador:N", alt.Tooltip("valor:Q", format=".2f")]
                ).properties(height=165, title=indicador)
            )
        if charts_contexto:
            st.altair_chart(alt.vconcat(*charts_contexto).resolve_scale(y="independent"), use_container_width=True)

with tab_macro:
    st.markdown('<div class="section-title">Datos macro diarios</div>', unsafe_allow_html=True)
    clases_dia = filtro_clases
    tipos_dia = filtro_tipos
    comuna_dia = filtro_comuna
    st.caption(f"Filtro aplicado · Comuna: {comuna_dia}")
    peu_fechas_dia = peu[(peu["clase_ruta"].isin(clases_dia)) & (peu["tipo_ruta"].isin(tipos_dia))].copy()
    if comuna_dia and comuna_dia != "Todas":
        peu_fechas_dia = peu_fechas_dia[peu_fechas_dia["comuna"].eq(comuna_dia)]
    fechas_dia = sorted(peu_fechas_dia["fecha_operacion"].dropna().unique())
    fecha_sel = excel_single_filter("Día", fechas_dia, "fecha_macro", fechas_dia[0] if fechas_dia else None)
    if not fecha_sel:
        st.warning("No hay fechas disponibles."); st.stop()
    with st.spinner("Calculando baseline y simulaciones automáticamente..."):
        sim = ejecutar_escenarios_dia(fecha_sel, clases_dia, tipos_dia, comuna_dia)
    st.session_state["sim_dia"] = sim

    fecha_mostrar = pd.to_datetime(fecha_sel).strftime("%d-%m-%Y")
    met_dia = sim["metrics"][sim["metrics"]["escenario"].eq("Baseline")].iloc[0]
    st.markdown(f'''
    <div class="day-card">
      <div class="day-card-title">Día operacional seleccionado</div>
      <div class="day-card-date">{fecha_mostrar}</div>
      <div class="day-card-detail">
        Comuna: <b>{comuna_dia}</b> · Reservas: <b>{num(met_dia.get('reservas'))}</b> · OS: <b>{num(met_dia.get('total_os'))}</b> · Volumen: <b>{dec(met_dia.get('volumen'))} m³</b> · Rutas baseline: <b>{num(met_dia.get('cantidad_rutas'))}</b>
      </div>
    </div>
    ''', unsafe_allow_html=True)
    st.info("Baseline representa la operación real. Los escenarios optimizados permiten comparar mejoras sobre la misma demanda operativa disponible.")
    estados_ok = {"ok", "ok_heuristica", "ok_con_puntos_diagnostico", "ok_ruta_preservada"}
    if sim.get("meta_ruta", {}).get("status") not in estados_ok or sim.get("meta_cap", {}).get("status") not in estados_ok:
        st.warning("No fue posible generar todos los escenarios con los filtros seleccionados. Revisa la demanda disponible y vuelve a intentar.")
    else:
        st.success("Escenarios generados correctamente.")

    macro = sim["metrics"].copy()
    cols_show = ["escenario","cantidad_rutas","total_os","cumplimiento_vh","vueltas","volumen","capacidad_total","excepciones","reservas","productividad","factor_ocupacion","ocupacion_vehiculo_dia","tiempo_ruta"]
    pct_cols = ["cumplimiento_vh", "excepciones", "factor_ocupacion", "ocupacion_vehiculo_dia"]
    fmt = {c: "{:.1f}%" for c in pct_cols if c in macro.columns}
    fmt.update({"productividad": "{:.1f}", "tiempo_ruta": "{:.1f}", "volumen": "{:.3f}", "capacidad_total": "{:.3f}", "total_os":"{:.0f}", "reservas":"{:.0f}"})

    st.markdown("#### Cuadro comparativo")
    card_cols = st.columns(3)
    for idx, (_, row) in enumerate(macro.iterrows()):
        with card_cols[idx]:
            st.markdown(f"""
<div style="border:1px solid #EAECF0;border-radius:16px;padding:14px;background:#FFFFFF;min-height:270px;">
  <div style="font-size:1.05rem;font-weight:800;margin-bottom:8px;">{row['escenario']}</div>
  <div style="font-size:0.85rem;color:#667085;margin-bottom:12px;">Resultados del día seleccionado</div>
  <div><b>Rutas:</b> {num(row['cantidad_rutas'])}</div>
  <div><b>Vueltas:</b> {num(row['vueltas'])}</div>
  <div><b>Cumplimiento VH:</b> {pct(row['cumplimiento_vh'])}</div>
  <div><b>Ocupación:</b> {pct(row['factor_ocupacion'])}</div>
  <div><b>Productividad:</b> {dec(row['productividad'])}</div>
  <div><b>Tiempo ruta:</b> {dec(row['tiempo_ruta'])} min</div>
  <hr style="border:none;border-top:1px solid #EAECF0;margin:10px 0;">
  <div><b>OS:</b> {num(row['total_os'])}</div>
  <div><b>Reservas:</b> {num(row['reservas'])}</div>
  <div><b>Volumen m³:</b> {dec(row['volumen'])}</div>
</div>
""", unsafe_allow_html=True)

    st.markdown("#### Tabla detalle")
    st.dataframe(macro[cols_show].style.format(fmt, na_rep="-"), use_container_width=True, hide_index=True)

    b, r, c = macro.iloc[0], macro.iloc[1], macro.iloc[2]
    k1,k2,k3,k4 = st.columns(4)
    k1.metric("Rutas baseline", num(b["cantidad_rutas"]))
    k2.metric("Rutas opt. ruta", num(r["cantidad_rutas"]), delta=num(r["cantidad_rutas"]-b["cantidad_rutas"]))
    k3.metric("Rutas opt. capacidades", num(c["cantidad_rutas"]), delta=num(c["cantidad_rutas"]-b["cantidad_rutas"]))
    k4.metric("OS del día", num(b["total_os"]))
    st.caption("Baseline muestra el contexto completo. Los escenarios optimizados muestran demanda PU optimizable; por eso reservas/OS pueden bajar si había excepciones o puntos sin coordenadas.")

with tab_rutas:
    st.markdown('<div class="section-title">Análisis por ruta / mapas OSRM</div>', unsafe_allow_html=True)
    st.caption(f"Filtro aplicado · Comuna: {filtro_comuna}")
    sim = st.session_state.get("sim_dia")
    if sim is None:
        st.warning("Primero entra a Datos macro diarios para seleccionar el día."); st.stop()
    sub1, sub2, sub3, sub4 = st.tabs(["Baseline", "Optimización de ruta", "Optimización de capacidades", "Tablas"])
    with sub1:
        stops_bl, paths_bl = preparar_mapa_baseline(sim["baseline"], cols)
        if not stops_bl.empty:
            stops_bl["id_punto_opt"] = stops_bl[cols["col_reserva"]].astype(str) if cols["col_reserva"] else "-"
            stops_bl["vehiculo_base"] = stops_bl["ruta"]; stops_bl["eta"] = stops_bl["datetime_gestion"].dt.strftime("%H:%M")
            stops_bl["os"] = pd.to_numeric(stops_bl[cols["col_q_os"]], errors="coerce").fillna(0) if cols["col_q_os"] else 1
            if cols["col_km"]:
                q_os_mapa = pd.to_numeric(stops_bl[cols["col_q_os"]], errors="coerce").fillna(1) if cols["col_q_os"] else pd.Series(1, index=stops_bl.index)
                ajuste_mapa = ajustar_volumen_m3(stops_bl[cols["col_km"]], q_os_mapa, capmax_por_os=stops_bl["ruta"].map(dicruta.set_index("ruta")["capmax"].to_dict()) if "capmax" in dicruta.columns else None, umbral_m3_por_os=3.0, m3_estandar_por_os=0.33)
                stops_bl["volumen_original_m3"] = ajuste_mapa["volumen_original_m3"]
                stops_bl["volumen"] = ajuste_mapa["volumen_m3"]
                stops_bl["m3_por_os"] = ajuste_mapa["m3_por_os"]
                stops_bl["volumen_ajustado"] = ajuste_mapa["volumen_ajustado"]
            else:
                stops_bl["volumen_original_m3"] = 0
                stops_bl["volumen"] = 0
                stops_bl["m3_por_os"] = 0
                stops_bl["volumen_ajustado"] = False
        render_map(stops_bl, paths_bl, "Mapa Baseline por calles OSRM", key_prefix="mapa_baseline")
    with sub2:
        stops_r, paths_r = preparar_mapa_opt(sim["routes_ruta"])
        render_map(stops_r, paths_r, "Mapa Optimización de ruta por calles OSRM", key_prefix="mapa_opt_ruta")
    with sub3:
        stops_c, paths_c = preparar_mapa_opt(sim["routes_cap"])
        render_map(stops_c, paths_c, "Mapa Optimización de capacidades por calles OSRM", key_prefix="mapa_opt_cap")
    with sub4:
        st.markdown("#### Resumen por ruta / vuelta")
        resumen_bl_ruta = resumen_por_ruta_baseline(sim["baseline"], dicruta, cols)
        resumen_bl_vuelta = resumen_por_vuelta_baseline(sim["baseline"], dicruta, cols)
        t1,t2,t3,t4 = st.tabs(["Baseline ruta", "Baseline vuelta", "Opt. ruta", "Opt. capacidades"])
        with t1: st.dataframe(resumen_bl_ruta, use_container_width=True)
        with t2: st.dataframe(resumen_bl_vuelta, use_container_width=True)
        with t3: st.dataframe(sim["resumen_ruta_opt"], use_container_width=True)
        with t4: st.dataframe(sim["resumen_cap_opt"], use_container_width=True)

with tab_exportar:
    st.markdown('<div class="section-title">Exportar</div>', unsafe_allow_html=True)
    st.caption("Ahora puedes descargar uno o varios días, no solo el día que estás viendo en pantalla.")

    clases_exp = filtro_clases
    tipos_exp = filtro_tipos
    comuna_exp = filtro_comuna
    st.info(f"Exportar usará los filtros generales aplicados · Comuna: {comuna_exp}")

    peu_fechas_exp = peu[(peu["clase_ruta"].isin(clases_exp)) & (peu["tipo_ruta"].isin(tipos_exp))].copy()
    if comuna_exp and comuna_exp != "Todas":
        peu_fechas_exp = peu_fechas_exp[peu_fechas_exp["comuna"].eq(comuna_exp)]
    fechas_exp = sorted(peu_fechas_exp["fecha_operacion"].dropna().unique())
    fechas_sel_exp = st.multiselect("Días a descargar", fechas_exp, default=fechas_exp[:1], format_func=lambda x: str(x))

    if not fechas_sel_exp:
        st.warning("Selecciona al menos un día para descargar.")
    else:
        st.info(f"Se exportarán {len(fechas_sel_exp)} día(s).")

        if st.button("Preparar Excel de descarga", type="primary"):
            sims=[]
            prog = st.progress(0, text="Preparando descarga...")
            for i, f in enumerate(fechas_sel_exp):
                sim_tmp = ejecutar_escenarios_dia(f, clases_exp, tipos_exp, comuna_exp)
                sims.append((f, sim_tmp))
                prog.progress((i+1)/len(fechas_sel_exp), text=f"Días preparados: {i+1}/{len(fechas_sel_exp)}")
            prog.empty()

            output = "baseline_optimizador_pm_export.xlsx"
            all_metrics=[]; all_baseline=[]; all_bl_ruta=[]; all_bl_vuelta=[]
            all_opt_ruta_res=[]; all_opt_ruta_det=[]; all_opt_cap_res=[]; all_opt_cap_det=[]; all_puntos=[]

            for f, sim_tmp in sims:
                all_metrics.append(sim_tmp["metrics"].assign(fecha_export=f))
                all_baseline.append(sim_tmp["baseline"].assign(fecha_export=f))
                all_bl_ruta.append(resumen_por_ruta_baseline(sim_tmp["baseline"], dicruta, cols).assign(fecha_export=f))
                all_bl_vuelta.append(resumen_por_vuelta_baseline(sim_tmp["baseline"], dicruta, cols).assign(fecha_export=f))
                all_opt_ruta_res.append(sim_tmp["resumen_ruta_opt"].assign(fecha_export=f))
                all_opt_ruta_det.append(sim_tmp["detail_ruta"].assign(fecha_export=f))
                all_opt_cap_res.append(sim_tmp["resumen_cap_opt"].assign(fecha_export=f))
                all_opt_cap_det.append(sim_tmp["detail_cap"].assign(fecha_export=f))
                all_puntos.append(sim_tmp["puntos"].assign(fecha_export=f))

            with pd.ExcelWriter(output, engine="openpyxl") as writer:
                pd.concat(all_metrics, ignore_index=True).to_excel(writer, sheet_name="macro_escenarios", index=False)
                pd.concat(all_baseline, ignore_index=True).to_excel(writer, sheet_name="baseline_detalle", index=False)
                pd.concat(all_bl_ruta, ignore_index=True).to_excel(writer, sheet_name="baseline_resumen_ruta", index=False)
                pd.concat(all_bl_vuelta, ignore_index=True).to_excel(writer, sheet_name="baseline_resumen_vuelta", index=False)
                pd.concat(all_opt_ruta_res, ignore_index=True).to_excel(writer, sheet_name="opt_ruta_resumen", index=False)
                pd.concat(all_opt_ruta_det, ignore_index=True).to_excel(writer, sheet_name="opt_ruta_detalle", index=False)
                pd.concat(all_opt_cap_res, ignore_index=True).to_excel(writer, sheet_name="opt_cap_resumen", index=False)
                pd.concat(all_opt_cap_det, ignore_index=True).to_excel(writer, sheet_name="opt_cap_detalle", index=False)
                pd.concat(all_puntos, ignore_index=True).to_excel(writer, sheet_name="demanda_optimizable", index=False)
                dicruta.to_excel(writer, sheet_name="dicruta_capacidad", index=False)

            with open(output, "rb") as f:
                st.download_button("Descargar Excel completo", data=f, file_name=output, mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
