import os
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE) if os.path.basename(HERE) == 'streamlit_app' else HERE
sys.path.insert(0, REPO)

import streamlit as st
import pandas as pd
from concurrent.futures import ThreadPoolExecutor


def _credenciales():
    """Lee SUPABASE_URL/SUPABASE_KEY de Streamlit secrets; cae a .env local en desarrollo."""
    url = key = None
    try:
        url = st.secrets['SUPABASE_URL']
        key = st.secrets['SUPABASE_KEY']
    except Exception:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(REPO, '.env'))
        url, key = os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_KEY')
    if url:
        os.environ['SUPABASE_URL'] = str(url).strip()
    if key:
        os.environ['SUPABASE_KEY'] = str(key).strip()
    return url, key


SUPABASE_URL, SUPABASE_KEY = _credenciales()
if not SUPABASE_URL or not SUPABASE_KEY:
    try:
        disponibles = ', '.join([k for k in st.secrets if k.lower() not in ('supabase_url', 'supabase_key')]) or '(ninguna)'
    except Exception:
        disponibles = '(no se pudo leer st.secrets)'
    st.error('Faltan credenciales de Supabase: define SUPABASE_URL y SUPABASE_KEY '
             'en los Secrets de Streamlit Cloud (o en .env local). '
             f'Claves presentes en st.secrets: {disponibles}')
    st.stop()

from app import Api

st.set_page_config(page_title='Dashboard Lefcom', layout='wide', page_icon='📊')

api = Api()

MESES = ['Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio',
         'Julio', 'Agosto', 'Septiembre', 'Octubre', 'Noviembre', 'Diciembre']


@st.cache_data(ttl=300, show_spinner=False)
def _get_filtros():
    return api.get_filtros()


@st.cache_data(ttl=60, show_spinner=False)
def _get_opciones(anio, mes):
    return api.get_opciones(anio, mes)


def _df_int(rows):
    df = pd.DataFrame(rows)
    for c in df.columns:
        try:
            df[c] = pd.to_numeric(df[c]).astype(int)
        except (TypeError, ValueError):
            pass
    return df


def _render(res, titulo):
    st.subheader(titulo)
    if not res.get('success'):
        st.error(res.get('error', 'Error al consultar.'))
        return
    rows = res.get('rows')
    if not rows:
        st.info('No hay ventas en este periodo.')
        return
    st.dataframe(_df_int(rows), width='stretch', hide_index=True)


filtros = _get_filtros()
if not filtros.get('success'):
    st.error(filtros.get('error', 'No se pudieron cargar los filtros.'))
    st.stop()

anios = filtros['anios']
hoy = date.today()

with st.sidebar:
    st.header('Filtros')
    anio_sel = st.selectbox('Año', anios, index=anios.index(hoy.year) if hoy.year in anios else 0)
    mes_sel = st.selectbox('Mes', MESES, index=hoy.month - 1)
    mes = MESES.index(mes_sel) + 1

    opts = _get_opciones(anio_sel, mes)
    if not opts.get('success'):
        st.error(opts.get('error', 'Error al cargar opciones.'))
        asesores, cps_list, financieras, marcas, productos = [], [], [], [], []
    else:
        asesores, cps_list = opts['asesores'], opts['cps']
        financieras, marcas, productos = opts['financieras'], opts['marcas'], opts['productos']

    asesor_sel = st.selectbox('Asesor', ['Todos'] + asesores)
    cps_sel = st.selectbox('CPS', ['Todos'] + cps_list)
    finan_sel = st.selectbox('Financiera', ['Todas'] + financieras)
    marca_sel = st.selectbox('Marca', ['Todas'] + marcas)
    producto_sel = st.selectbox('Producto', ['Todos'] + productos)

asesor = None if asesor_sel == 'Todos' else asesor_sel
cps = None if cps_sel == 'Todos' else cps_sel
financiera = None if finan_sel == 'Todas' else finan_sel
marca = None if marca_sel == 'Todas' else marca_sel
producto = None if producto_sel == 'Todos' else producto_sel


def _cargar_reporte(metodo):
    return metodo(anio_sel, mes, asesor, cps, financiera, marca, producto)


REPORTES = [
    api.get_reporte_asesor_producto,
    api.get_reporte_financieras,
    api.get_reporte_productos,
    api.get_reporte_marcas,
    api.get_reporte_cps,
    api.get_reporte_planes,
    api.get_reporte_referencias,
    api.get_reporte_ingresos,
]

tab_resumen, tab_hist = st.tabs(['📊 Resumen', '🕘 Historial'])

with tab_resumen:
    with st.spinner('Cargando reportes...'):
        with ThreadPoolExecutor(max_workers=8) as pool:
            res_as, res_fin, res_prod, res_marca, res_cps, res_plan, res_ref, res_ing = list(
                pool.map(_cargar_reporte, REPORTES))

    c1, c2 = st.columns([0.65, 0.35])
    with c1:
        _render(res_as, f"Conteo de ventas por asesor y producto — {MESES[mes - 1]} de {anio_sel}"
                        f"  ({len(res_as.get('rows', [])) if res_as.get('success') else 0} asesores)")
    with c2:
        _render(res_marca, "Ventas por Marca — " + (res_marca.get('mes_actual_label') or ''))

    c3, c4 = st.columns([0.65, 0.35])
    with c3:
        _render(res_prod, "Ventas por Producto — " + (res_prod.get('mes_actual_label') or '') +
                ("  (año anterior: " + res_prod.get('anio_anterior_label', '') + ")" if res_prod.get('anio_anterior_label') else ''))
    with c4:
        _render(res_fin, "Ventas por Financiera — " + (res_fin.get('mes_actual_label') or ''))

    c5, c6 = st.columns(2)
    with c5:
        _render(res_cps, "Marcas por CPS — " + (res_cps.get('mes_actual_label') or ''))
    with c6:
        _render(res_plan, "Planes Vendidos (Postpago) — " + (res_plan.get('mes_actual_label') or ''))

    c7, c8 = st.columns(2)
    with c7:
        _render(res_ref, "Ventas por Referencia — " + (res_ref.get('mes_actual_label') or ''))
    with c8:
        _render(res_ing, "Ingreso por Equipos — " + (res_ing.get('mes_actual_label') or ''))

with tab_hist:
    tipo_hist = st.radio('Historial', ['Producto', 'PDV', 'Marca', 'Asesor'], horizontal=True)
    metodos_hist = {
        'Producto': api.get_historial_productos,
        'PDV': api.get_historial_pdv,
        'Marca': api.get_historial_marca,
        'Asesor': api.get_historial_asesor,
    }
    with st.spinner(f'Cargando historial de {tipo_hist.lower()}...'):
        hist = metodos_hist[tipo_hist]()
    if not hist.get('success'):
        st.error(hist.get('error', 'Error al cargar el historial.'))
    else:
        st.subheader(f'Historial de ventas por {tipo_hist} (año a año)'
                     + (f'  ({len(hist["rows"])} registros)' if hist['rows'] else ''))
        if not hist['rows']:
            st.info('No hay datos de historial.')
        else:
            df_hist = pd.DataFrame(hist['rows'])
            df_hist = df_hist.rename(columns={df_hist.columns[0]: tipo_hist})
            for col in df_hist.columns:
                if col != tipo_hist:
                    df_hist[col] = df_hist[col].astype(int)
            st.dataframe(df_hist, width='stretch', hide_index=True)