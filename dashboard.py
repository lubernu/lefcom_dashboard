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

DIAS = ['asesor', 'cps', 'financiera', 'marca', 'producto']

for d in DIAS:
    if f'f_{d}' not in st.session_state:
        st.session_state[f'f_{d}'] = 'Todo'
if '_pend' not in st.session_state:
    st.session_state['_pend'] = {}

if st.session_state['_pend']:
    for d, v in st.session_state['_pend'].items():
        st.session_state[f'f_{d}'] = v
    st.session_state['_pend'] = {}


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


def _render(res, titulo, key=None, col_click=None, propia=None):
    st.subheader(titulo)
    if not res.get('success'):
        st.error(res.get('error', 'Error al consultar.'))
        return
    rows = res.get('rows')
    if not rows:
        st.info('No hay ventas en este periodo.')
        return
    df = _df_int(rows)
    if key is None:
        st.dataframe(df, width='stretch', hide_index=True)
        return
    ev = st.dataframe(df, width='stretch', hide_index=True, on_select='rerun',
                      selection_mode='single-row', key=key)
    if col_click and propia and ev is not None and ev.selection is not None and ev.selection.rows:
        idx = int(ev.selection.rows[0])
        if idx < len(df):
            valor = str(df.iloc[idx][col_click])
            if valor.strip().upper() != 'TOTAL':
                proc = st.session_state.setdefault('_click_proc', {})
                clave = tuple(ev.selection.rows)
                if proc.get(key) != clave:
                    proc[key] = clave
                    st.session_state['_pend'][propia] = valor
                    st.rerun()


@st.cache_data(ttl=90, show_spinner=False)
def _reporte(nombre, anio, mes, asesor, cps, financiera, marca, producto):
    return getattr(api, 'get_reporte_' + nombre)(anio, mes, asesor, cps, financiera, marca, producto)


# (nombre, columna del click, dimensión que filtra)
CONFIG = [
    ('asesor_producto', 'asesor', 'asesor'),
    ('marcas',          'marca',  'marca'),
    ('productos',       'producto', 'producto'),
    ('financieras',     'metodo', 'financiera'),
    ('cps',             'cps',    'cps'),
    ('planes',          None,     None),
    ('referencias',     None,     None),
    ('ingresos',        'producto', 'producto'),
]


def _ff(dim):
    """Valores de filtro con la dimensión propia excluida (None = no filtra)."""
    return (
        None if dim == 'asesor' else st.session_state['f_asesor'],
        None if dim == 'cps' else st.session_state['f_cps'],
        None if dim == 'financiera' else st.session_state['f_financiera'],
        None if dim == 'marca' else st.session_state['f_marca'],
        None if dim == 'producto' else st.session_state['f_producto'],
    )


def _a_none(v):
    return None if v == 'Todo' else v


def _cargar_todos(anio_sel, mes):
    resultados = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {}
        for nombre, _, dim in CONFIG:
            a, c, fin, m, p = _ff(dim)
            futures[nombre] = pool.submit(_reporte, nombre, anio_sel, mes,
                                          _a_none(a), _a_none(c), _a_none(fin),
                                          _a_none(m), _a_none(p))
        for nombre, fut in futures.items():
            resultados[nombre] = fut.result()
    return resultados


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

    if mes != st.session_state.get('_mes_cache') or anio_sel != st.session_state.get('_anio_cache'):
        st.session_state['_mes_cache'] = mes
        st.session_state['_anio_cache'] = anio_sel
        for d in DIAS:
            st.session_state[f'f_{d}'] = 'Todo'

    opts = _get_opciones(anio_sel, mes)
    if not opts.get('success'):
        st.error(opts.get('error', 'Error al cargar opciones.'))
        asesores, cps_list, financieras, marcas, productos = [], [], [], [], []
    else:
        asesores, cps_list = opts['asesores'], opts['cps']
        financieras, marcas, productos = opts['financieras'], opts['marcas'], opts['productos']

    st.selectbox('Asesor', ['Todo'] + asesores, key='f_asesor')
    st.selectbox('CPS', ['Todo'] + cps_list, key='f_cps')
    st.selectbox('Financiera', ['Todo'] + financieras, key='f_financiera')
    st.selectbox('Marca', ['Todo'] + marcas, key='f_marca')
    st.selectbox('Producto', ['Todo'] + productos, key='f_producto')

    activos = {d: st.session_state[f'f_{d}'] for d in DIAS if st.session_state[f'f_{d}'] != 'Todo'}
    if activos:
        st.write('**Filtros activos**')
        for d, v in activos.items():
            c1, c2 = st.columns([3, 1])
            c1.caption(f'{d.capitalize()}: {v}')
            if c2.button('✕', key=f'clear_{d}'):
                st.session_state['_pend'][d] = 'Todo'
                st.rerun()

tab_resumen, tab_hist = st.tabs(['📊 Resumen', '🕘 Historial'])

with tab_resumen:
    with st.spinner('Cargando reportes...'):
        R = _cargar_todos(anio_sel, mes)

    c1, c2 = st.columns([0.65, 0.35])
    with c1:
        _render(R['asesor_producto'],
                f"Conteo de ventas por asesor y producto — {MESES[mes - 1]} de {anio_sel}"
                f"  ({len(R['asesor_producto'].get('rows', [])) if R['asesor_producto'].get('success') else 0} asesores)",
                key='df_as', col_click='asesor', propia='asesor')
    with c2:
        _render(R['marcas'], "Ventas por Marca — " + (R['marcas'].get('mes_actual_label') or ''),
                key='df_marca', col_click='marca', propia='marca')

    c3, c4 = st.columns([0.65, 0.35])
    with c3:
        _render(R['productos'], "Ventas por Producto — " + (R['productos'].get('mes_actual_label') or '') +
                ("  (año anterior: " + R['productos'].get('anio_anterior_label', '') + ")" if R['productos'].get('anio_anterior_label') else ''),
                key='df_prod', col_click='producto', propia='producto')
    with c4:
        _render(R['financieras'], "Ventas por Financiera — " + (R['financieras'].get('mes_actual_label') or ''),
                key='df_fin', col_click='metodo', propia='financiera')

    c5, c6 = st.columns(2)
    with c5:
        _render(R['cps'], "Marcas por CPS — " + (R['cps'].get('mes_actual_label') or ''),
                key='df_cps', col_click='cps', propia='cps')
    with c6:
        _render(R['planes'], "Planes Vendidos (Postpago) — " + (R['planes'].get('mes_actual_label') or ''))

    c7, c8 = st.columns(2)
    with c7:
        _render(R['referencias'], "Ventas por Referencia — " + (R['referencias'].get('mes_actual_label') or ''))
    with c8:
        _render(R['ingresos'], "Ingreso por Equipos — " + (R['ingresos'].get('mes_actual_label') or ''),
                key='df_ing', col_click='producto', propia='producto')

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