import os
import httpx
import webview
from supabase import create_client, Client
from supabase.lib.client_options import SyncClientOptions
from dotenv import load_dotenv
from datetime import date, datetime, timezone
import calendar

import pandas as pd

# 1. Cargar las variables de entorno desde el archivo .env
load_dotenv()

# 2. Obtener las credenciales de forma segura
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Validación de seguridad: asegurar que las claves existan
if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("⚠️ Error: Faltan las credenciales de Supabase en el archivo .env")

# 3. Inicializar cliente de Supabase (HTTP/1.1: HTTP/2 da WinError 10035 en Windows con sockets no bloqueantes)
supabase: Client = create_client(
    SUPABASE_URL.strip(),
    SUPABASE_KEY.strip(),
    options=SyncClientOptions(httpx_client=httpx.Client(http2=False)),
)

TAM_PAGINA = 1000  # límite máximo por petición de PostgREST

MESES = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
         "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]


class Api:
    def _consultar_tabla(self, columnas, fecha_min=None, fecha_max=None, asesor=None, cps=None,
                         financiera=None, marca=None, producto=None):
        """Trae todas las filas (pagina de a 1000) aplicando filtros en el servidor."""
        query = supabase.table('ventas_lefcom').select(','.join(columnas))
        if fecha_min is not None:
            query = query.gte('fecha', fecha_min.isoformat())
        if fecha_max is not None:
            query = query.lt('fecha', fecha_max.isoformat())
        if asesor:
            query = query.eq('nombre_asesor', asesor)
        if cps:
            query = query.eq('cps', cps)
        if financiera:
            query = query.eq('metodo_pago', financiera)
        if marca:
            query = query.eq('marca', marca)
        if producto:
            query = query.eq('producto', producto)

        datos = []
        offset = 0
        while True:
            pagina = query.range(offset, offset + TAM_PAGINA - 1).execute().data
            if not pagina:
                break
            datos.extend(pagina)
            offset += len(pagina)
            if len(pagina) < TAM_PAGINA:
                break
        return datos

    def _existe_anio(self, anio):
        data = (supabase.table('ventas_lefcom')
                .select('fecha')
                .gte('fecha', f'{anio}-01-01T00:00:00+00:00')
                .lt('fecha', f'{anio + 1}-01-01T00:00:00+00:00')
                .limit(1)
                .execute().data)
        return bool(data)

    def _rango_mes(self, anio, mes):
        inicio = datetime(anio, mes, 1, tzinfo=timezone.utc)
        fin = datetime(anio + 1, 1, 1, tzinfo=timezone.utc) if mes == 12 else datetime(anio, mes + 1, 1, tzinfo=timezone.utc)
        return inicio, fin

    def _contar_columna_db(self, col, inicio, fin, asesor=None, cps=None, filtro=None,
                           financiera=None, marca=None, producto=None):
        """Conteo GROUP BY en la BD (RPC contar_columna; acepta financiera/marca/producto);
        None si la función no existe o falla (se cae al fallback pandas)."""
        try:
            resp = supabase.rpc('contar_columna', {
                'p_col': col,
                'p_desde': inicio.isoformat(),
                'p_hasta': fin.isoformat(),
                'p_asesor': asesor,
                'p_cps': cps,
                'p_filtro': filtro,
                'p_financiera': financiera,
                'p_marca': marca,
                'p_producto': producto,
            }).execute()
        except Exception:
            return None
        data = resp.data or []
        return pd.Series({d['clave']: int(d['n']) for d in data})

    def _sumar_producto_db(self, inicio, fin, asesor=None, cps=None,
                           financiera=None, marca=None, producto=None):
        """SUM(vr_unitario) GROUP BY producto en la BD (acepta financiera/marca/producto);
        None si la función no existe o falla (se cae al fallback pandas)."""
        try:
            resp = supabase.rpc('sumar_producto', {
                'p_desde': inicio.isoformat(),
                'p_hasta': fin.isoformat(),
                'p_asesor': asesor,
                'p_cps': cps,
                'p_financiera': financiera,
                'p_marca': marca,
                'p_producto': producto,
            }).execute()
        except Exception:
            return None
        data = resp.data or []
        return pd.Series({d['clave']: float(d['n']) for d in data})

    @staticmethod
    def _normalizar_serie(serie, valor_sin):
        idx = [valor_sin if x is None else x for x in serie.index]
        serie.index = idx
        return serie

    def get_filtros(self):
        try:
            from concurrent.futures import ThreadPoolExecutor
            candidatos = list(range(date.today().year + 1, 2015, -1))
            with ThreadPoolExecutor(max_workers=8) as pool:
                resultados = list(pool.map(self._existe_anio, candidatos))
            anios = [a for a, existe in zip(candidatos, resultados) if existe]
            if not anios:
                return {"success": False, "error": "No hay datos en la tabla"}
            return {"success": True, "anios": anios, "meses": MESES}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _historial_por(self, columna, clave, valor_sin, excluir_traido=False):
        filtros = self.get_filtros()
        if not filtros["success"]:
            return {"success": False, "error": filtros.get("error", "No hay datos en la tabla")}
        anios = filtros["anios"]

        anuales = []
        for anio in anios:
            inicio = datetime(anio, 1, 1, tzinfo=timezone.utc)
            fin = datetime(anio + 1, 1, 1, tzinfo=timezone.utc)
            serie = self._contar_columna_db(columna, inicio, fin,
                                            filtro='no_traido' if excluir_traido else None)
            if serie is None:
                columnas = [columna] + ([] if columna == 'marca' else [])
                if excluir_traido and 'marca' not in columnas:
                    columnas.append('marca')
                datos = self._consultar_tabla(columnas, fecha_min=inicio, fecha_max=fin)
                serie = pd.Series(dtype='int64')
                if datos:
                    df = pd.DataFrame(datos)
                    if excluir_traido:
                        df = df[df['marca'].astype(str).str.upper() != 'TRAIDO']
                    serie = df[columna].value_counts()
            serie = self._normalizar_serie(serie, valor_sin)
            serie.index = [str(x) for x in serie.index]
            anuales.append(serie)

        categorias = sorted(set().union(*[set(s.index) for s in anuales]))
        rows = []
        for c in categorias:
            row = {clave: c}
            for anio, serie in zip(anios, anuales):
                row[str(anio)] = int(serie.get(c, 0))
            rows.append(row)

        return {"success": True, "anios": [str(a) for a in anios], "rows": rows}

    def get_historial_productos(self):
        try:
            return self._historial_por('producto', 'producto', 'Sin categoría')
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_historial_pdv(self):
        try:
            return self._historial_por('cps', 'pdv', 'Sin PDV', excluir_traido=True)
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_historial_marca(self):
        try:
            return self._historial_por('marca', 'marca', 'Sin marca', excluir_traido=True)
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_historial_asesor(self):
        try:
            return self._historial_por('nombre_asesor', 'asesor', 'Sin asesor')
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_opciones(self, anio=None, mes=None):
        try:
            hoy = date.today()
            anio = int(anio) if anio is not None else hoy.year
            mes = int(mes) if mes is not None else hoy.month

            inicio = datetime(anio, mes, 1, tzinfo=timezone.utc)
            fin = datetime(anio + 1, 1, 1, tzinfo=timezone.utc) if mes == 12 else datetime(anio, mes + 1, 1, tzinfo=timezone.utc)
            datos = self._consultar_tabla(['nombre_asesor', 'cps', 'metodo_pago', 'marca', 'producto'],
                                  fecha_min=inicio, fecha_max=fin)
            df = pd.DataFrame(datos)
            if df.empty:
                return {"success": True, "asesores": [], "cps": [], "financieras": [], "marcas": [], "productos": []}
            df['nombre_asesor'] = df['nombre_asesor'].fillna('Sin asesor')
            df['cps'] = df['cps'].fillna('Sin CPS')
            df['metodo_pago'] = df['metodo_pago'].fillna('Sin financiera')
            df['marca'] = df['marca'].fillna('Sin marca')
            df['producto'] = df['producto'].fillna('Sin categoría')
            df = df[df['marca'].astype(str).str.upper() != 'TRAIDO']
            asesores = sorted(df['nombre_asesor'].unique().tolist())
            cps_list = sorted(df['cps'].unique().tolist())
            financieras = sorted(df['metodo_pago'].unique().tolist())
            marcas = sorted(df['marca'].unique().tolist())
            productos = sorted(df['producto'].unique().tolist())
            return {"success": True, "asesores": asesores, "cps": cps_list,
                    "financieras": financieras, "marcas": marcas, "productos": productos}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_reporte_asesor_producto(self, anio=None, mes=None, asesor=None, cps=None,
                                    financiera=None, marca=None, producto=None):
        try:
            hoy = date.today()
            anio = int(anio) if anio is not None else hoy.year
            mes = int(mes) if mes is not None else hoy.month

            inicio = date(anio, mes, 1)
            fin = date(anio + 1, 1, 1) if mes == 12 else date(anio, mes + 1, 1)

            import datetime as _dt
            fecha_min = _dt.datetime(inicio.year, inicio.month, inicio.day, tzinfo=_dt.timezone.utc)
            fecha_max = _dt.datetime(fin.year, fin.month, fin.day, tzinfo=_dt.timezone.utc)

            datos = self._consultar_tabla(['fecha', 'nombre_asesor', 'producto'],
                                          fecha_min=fecha_min, fecha_max=fecha_max,
                                          asesor=asesor, cps=cps,
                                          financiera=financiera, marca=marca, producto=producto)
            df = pd.DataFrame(datos)
            if df.empty:
                return {"success": True, "products": [], "rows": [], "anio": anio, "mes": mes}

            df['fecha'] = pd.to_datetime(df['fecha'], utc=True, errors='coerce')
            df = df.dropna(subset=['fecha'])
            df['nombre_asesor'] = df['nombre_asesor'].fillna('Sin asesor')
            df['producto'] = df['producto'].fillna('Sin categoría')

            pivote = pd.crosstab(df['nombre_asesor'], df['producto'])
            pivote['Total'] = pivote.sum(axis=1)

            products = [c for c in pivote.columns if c != 'Total']
            rows = []
            for idx, row in pivote.iterrows():
                item = {"asesor": idx}
                for p in products:
                    item[p] = int(row[p])
                item["Total"] = int(row["Total"])
                rows.append(item)

            rows.sort(key=lambda r: r["Total"], reverse=True)
            return {"success": True, "products": products, "rows": rows, "anio": anio, "mes": mes}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _contar_metodo_pago(self, anio, mes, asesor=None, cps=None,
                            financiera=None, marca=None, producto=None):
        inicio, fin = self._rango_mes(anio, mes)
        serie = self._contar_columna_db('metodo_pago', inicio, fin, asesor, cps,
                                        financiera=financiera, marca=marca, producto=producto)
        if serie is None:
            datos = self._consultar_tabla(['metodo_pago'], fecha_min=inicio, fecha_max=fin,
                                          asesor=asesor, cps=cps,
                                          financiera=financiera, marca=marca, producto=producto)
            serie = pd.Series(dtype='int64')
            if datos:
                df = pd.DataFrame(datos)
                df = df.dropna(subset=['metodo_pago'])
                df = df[df['metodo_pago'].astype(str).str.strip() != '']
                serie = df['metodo_pago'].value_counts()
        serie = serie[serie.index.notna() & (serie.index.astype(str).str.strip() != '')]
        return serie

    def get_reporte_financieras(self, anio=None, mes=None, asesor=None, cps=None,
                                financiera=None, marca=None, producto=None):
        try:
            hoy = date.today()
            anio = int(anio) if anio is not None else hoy.year
            mes = int(mes) if mes is not None else hoy.month

            if mes == 1:
                mes_ant, anio_ant = 12, anio - 1
            else:
                mes_ant, anio_ant = mes - 1, anio

            serie_ant = self._contar_metodo_pago(anio_ant, mes_ant, asesor=asesor, cps=cps,
                                             financiera=financiera, marca=marca, producto=producto)
            serie_act = self._contar_metodo_pago(anio, mes, asesor=asesor, cps=cps,
                                                 financiera=financiera, marca=marca, producto=producto)

            metodos = sorted(set(serie_ant.index) | set(serie_act.index),
                             key=lambda x: int(serie_act.get(x, 0)), reverse=True)

            ultimo_dia = calendar.monthrange(anio, mes)[1]
            if (anio, mes) == (hoy.year, hoy.month):
                factor = ultimo_dia / max(hoy.day, 1)
            else:
                factor = 1.0

            rows = []
            suma_ant = suma_act = suma_proy = 0
            for m in metodos:
                ant = int(serie_ant.get(m, 0))
                act = int(serie_act.get(m, 0))
                proy = int(round(act * factor))
                rows.append({'metodo': m, 'mes_anterior': ant, 'mes_actual': act, 'proyeccion': proy})
                suma_ant += ant
                suma_act += act
                suma_proy += proy
            rows.append({'metodo': 'TOTAL', 'mes_anterior': suma_ant, 'mes_actual': suma_act, 'proyeccion': suma_proy})

            return {
                "success": True,
                "rows": rows,
                "mes_anterior_label": f"{MESES[mes_ant - 1]} {anio_ant}",
                "mes_actual_label": f"{MESES[mes - 1]} {anio}",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _contar_producto(self, anio, mes, asesor=None, cps=None,
                         financiera=None, marca=None, producto=None):
        inicio, fin = self._rango_mes(anio, mes)
        serie = self._contar_columna_db('producto', inicio, fin, asesor, cps,
                                        financiera=financiera, marca=marca, producto=producto)
        if serie is None:
            datos = self._consultar_tabla(['producto'], fecha_min=inicio, fecha_max=fin,
                                          asesor=asesor, cps=cps,
                                          financiera=financiera, marca=marca, producto=producto)
            serie = pd.Series(dtype='int64')
            if datos:
                df = pd.DataFrame(datos)
                df['producto'] = df['producto'].fillna('Sin categoría')
                serie = df['producto'].value_counts()
        return self._normalizar_serie(serie, 'Sin categoría')

    def get_reporte_productos(self, anio=None, mes=None, asesor=None, cps=None,
                              financiera=None, marca=None, producto=None):
        try:
            hoy = date.today()
            anio = int(anio) if anio is not None else hoy.year
            mes = int(mes) if mes is not None else hoy.month

            if mes == 1:
                mes_ant, anio_ant = 12, anio - 1
            else:
                mes_ant, anio_ant = mes - 1, anio
            anio_prev = anio - 1

            serie_anio_pre = self._contar_producto(anio_prev, mes, asesor=asesor, cps=cps,
                                               financiera=financiera, marca=marca, producto=producto)
            serie_ant = self._contar_producto(anio_ant, mes_ant, asesor=asesor, cps=cps,
                                              financiera=financiera, marca=marca, producto=producto)
            serie_act = self._contar_producto(anio, mes, asesor=asesor, cps=cps,
                                              financiera=financiera, marca=marca, producto=producto)

            productos = sorted(set(serie_anio_pre.index) | set(serie_ant.index) | set(serie_act.index),
                               key=lambda x: int(serie_act.get(x, 0)), reverse=True)

            ultimo_dia = calendar.monthrange(anio, mes)[1]
            if (anio, mes) == (hoy.year, hoy.month):
                factor = ultimo_dia / max(hoy.day, 1)
            else:
                factor = 1.0

            rows = []
            suma_ap = suma_ant = suma_act = suma_proy = 0
            for p in productos:
                ap = int(serie_anio_pre.get(p, 0))
                ant = int(serie_ant.get(p, 0))
                act = int(serie_act.get(p, 0))
                proy = int(round(act * factor))
                rows.append({'producto': p, 'anio_anterior': ap, 'mes_anterior': ant,
                             'cantidad': act, 'proyeccion': proy})
                suma_ap += ap
                suma_ant += ant
                suma_act += act
                suma_proy += proy
            rows.append({'producto': 'TOTAL', 'anio_anterior': suma_ap, 'mes_anterior': suma_ant,
                         'cantidad': suma_act, 'proyeccion': suma_proy})

            return {
                "success": True,
                "rows": rows,
                "anio_anterior_label": f"{MESES[mes - 1]} {anio_prev}",
                "mes_anterior_label": f"{MESES[mes_ant - 1]} {anio_ant}",
                "mes_actual_label": f"{MESES[mes - 1]} {anio}",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _contar_marca(self, anio, mes, asesor=None, cps=None,
                      financiera=None, marca=None, producto=None):
        inicio, fin = self._rango_mes(anio, mes)
        serie = self._contar_columna_db('marca', inicio, fin, asesor, cps,
                                        financiera=financiera, marca=marca, producto=producto)
        if serie is None:
            datos = self._consultar_tabla(['marca'], fecha_min=inicio, fecha_max=fin,
                                          asesor=asesor, cps=cps,
                                          financiera=financiera, marca=marca, producto=producto)
            serie = pd.Series(dtype='int64')
            if datos:
                df = pd.DataFrame(datos)
                df = df[df['marca'].astype(str).str.upper() != 'TRAIDO']
                df['marca'] = df['marca'].fillna('Sin marca')
                serie = df['marca'].value_counts()
        else:
            serie = serie[serie.index.astype(str).str.upper() != 'TRAIDO']
        return self._normalizar_serie(serie, 'Sin marca')

    def get_reporte_marcas(self, anio=None, mes=None, asesor=None, cps=None,
                           financiera=None, marca=None, producto=None):
        try:
            hoy = date.today()
            anio = int(anio) if anio is not None else hoy.year
            mes = int(mes) if mes is not None else hoy.month

            if mes == 1:
                mes_ant, anio_ant = 12, anio - 1
            else:
                mes_ant, anio_ant = mes - 1, anio

            serie_ant = self._contar_marca(anio_ant, mes_ant, asesor=asesor, cps=cps,
                                       financiera=financiera, marca=marca, producto=producto)
            serie_act = self._contar_marca(anio, mes, asesor=asesor, cps=cps,
                                           financiera=financiera, marca=marca, producto=producto)

            marcas = sorted(set(serie_ant.index) | set(serie_act.index),
                            key=lambda x: int(serie_act.get(x, 0)), reverse=True)

            ultimo_dia = calendar.monthrange(anio, mes)[1]
            if (anio, mes) == (hoy.year, hoy.month):
                factor = ultimo_dia / max(hoy.day, 1)
            else:
                factor = 1.0

            rows = []
            suma_ant = suma_act = suma_proy = 0
            for m in marcas:
                ant = int(serie_ant.get(m, 0))
                act = int(serie_act.get(m, 0))
                proy = int(round(act * factor))
                rows.append({'marca': m, 'mes_anterior': ant, 'cantidad': act, 'proyeccion': proy})
                suma_ant += ant
                suma_act += act
                suma_proy += proy
            rows.append({'marca': 'TOTAL', 'mes_anterior': suma_ant, 'cantidad': suma_act, 'proyeccion': suma_proy})

            return {
                "success": True,
                "rows": rows,
                "mes_anterior_label": f"{MESES[mes_ant - 1]} {anio_ant}",
                "mes_actual_label": f"{MESES[mes - 1]} {anio}",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _contar_plan(self, anio, mes, asesor=None, cps=None,
                     financiera=None, marca=None, producto=None):
        inicio, fin = self._rango_mes(anio, mes)
        serie = self._contar_columna_db('plan', inicio, fin, asesor, cps, filtro='postpago',
                                        financiera=financiera, marca=marca, producto=producto)
        if serie is None:
            datos = self._consultar_tabla(['plan', 'producto'], fecha_min=inicio, fecha_max=fin,
                                          asesor=asesor, cps=cps,
                                          financiera=financiera, marca=marca, producto=producto)
            serie = pd.Series(dtype='int64')
            if datos:
                df = pd.DataFrame(datos)
                df = df[df['producto'].astype(str).str.strip().str.upper() == 'POSTPAGO']
                df['plan'] = df['plan'].fillna('Sin plan')
                serie = df['plan'].value_counts()
        return self._normalizar_serie(serie, 'Sin plan')

    def get_reporte_planes(self, anio=None, mes=None, asesor=None, cps=None,
                           financiera=None, marca=None, producto=None):
        try:
            hoy = date.today()
            anio = int(anio) if anio is not None else hoy.year
            mes = int(mes) if mes is not None else hoy.month

            if mes == 1:
                mes_ant, anio_ant = 12, anio - 1
            else:
                mes_ant, anio_ant = mes - 1, anio

            serie_ant = self._contar_plan(anio_ant, mes_ant, asesor=asesor, cps=cps,
                                      financiera=financiera, marca=marca, producto=producto)
            serie_act = self._contar_plan(anio, mes, asesor=asesor, cps=cps,
                                          financiera=financiera, marca=marca, producto=producto)

            planes = sorted(set(serie_ant.index) | set(serie_act.index),
                            key=lambda x: int(serie_act.get(x, 0)), reverse=True)

            ultimo_dia = calendar.monthrange(anio, mes)[1]
            if (anio, mes) == (hoy.year, hoy.month):
                factor = ultimo_dia / max(hoy.day, 1)
            else:
                factor = 1.0

            rows = []
            suma_ant = suma_act = suma_proy = 0
            for p in planes:
                ant = int(serie_ant.get(p, 0))
                act = int(serie_act.get(p, 0))
                proy = int(round(act * factor))
                rows.append({'plan': p, 'mes_anterior': ant, 'cantidad': act, 'proyeccion': proy})
                suma_ant += ant
                suma_act += act
                suma_proy += proy
            rows.append({'plan': 'TOTAL', 'mes_anterior': suma_ant, 'cantidad': suma_act, 'proyeccion': suma_proy})

            return {
                "success": True,
                "rows": rows,
                "mes_anterior_label": f"{MESES[mes_ant - 1]} {anio_ant}",
                "mes_actual_label": f"{MESES[mes - 1]} {anio}",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_reporte_referencias(self, anio=None, mes=None, asesor=None, cps=None,
                                financiera=None, marca=None, producto=None):
        try:
            hoy = date.today()
            anio = int(anio) if anio is not None else hoy.year
            mes = int(mes) if mes is not None else hoy.month

            inicio, fin = self._rango_mes(anio, mes)
            serie = self._contar_columna_db('referencia', inicio, fin, asesor, cps,
                                        financiera=financiera, marca=marca, producto=producto)
            if serie is None:
                datos = self._consultar_tabla(['referencia'], fecha_min=inicio, fecha_max=fin,
                                              asesor=asesor, cps=cps,
                                              financiera=financiera, marca=marca, producto=producto)
                serie = pd.Series(dtype='int64')
                if datos:
                    df = pd.DataFrame(datos)
                    df['referencia'] = df['referencia'].fillna('Sin referencia')
                    serie = df['referencia'].value_counts()
            serie = self._normalizar_serie(serie, 'Sin referencia')

            refs = sorted(serie.index, key=lambda x: int(serie.get(x, 0)), reverse=True)
            rows = []
            suma = 0
            for r in refs:
                cant = int(serie.get(r, 0))
                rows.append({'referencia': r, 'cantidad': cant})
                suma += cant
            rows.append({'referencia': 'TOTAL', 'cantidad': suma})

            return {"success": True, "rows": rows, "mes_actual_label": f"{MESES[mes - 1]} {anio}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_reporte_ingresos(self, anio=None, mes=None, asesor=None, cps=None,
                             financiera=None, marca=None, producto=None):
        try:
            hoy = date.today()
            anio = int(anio) if anio is not None else hoy.year
            mes = int(mes) if mes is not None else hoy.month

            inicio, fin = self._rango_mes(anio, mes)
            suma = self._sumar_producto_db(inicio, fin, asesor, cps,
                                           financiera=financiera, marca=marca, producto=producto)
            if suma is None:
                datos = self._consultar_tabla(['producto', 'vr_unitario'], fecha_min=inicio, fecha_max=fin,
                                              asesor=asesor, cps=cps,
                                              financiera=financiera, marca=marca, producto=producto)
                suma = pd.Series(dtype='float64')
                if datos:
                    df = pd.DataFrame(datos)
                    df['producto'] = df['producto'].fillna('Sin categoría')
                    df['vr_unitario'] = pd.to_numeric(df['vr_unitario'], errors='coerce').fillna(0)
                    suma = df.groupby('producto')['vr_unitario'].sum()
            suma = self._normalizar_serie(suma, 'Sin categoría')
            suma = suma[suma != 0].sort_values(ascending=False)
            rows = []
            total = 0
            for p, v in suma.items():
                valor = round(float(v), 0)
                rows.append({'producto': p, 'valor': valor})
                total += valor
            rows.append({'producto': 'TOTAL', 'valor': total})

            return {"success": True, "rows": rows, "mes_actual_label": f"{MESES[mes - 1]} {anio}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _contar_cps(self, anio, mes, asesor=None, cps=None,
                    financiera=None, marca=None, producto=None):
        inicio, fin = self._rango_mes(anio, mes)
        serie = self._contar_columna_db('cps', inicio, fin, asesor, cps, filtro='no_traido',
                                        financiera=financiera, marca=marca, producto=producto)
        if serie is None:
            datos = self._consultar_tabla(['cps', 'marca'], fecha_min=inicio, fecha_max=fin,
                                          asesor=asesor, cps=cps,
                                          financiera=financiera, marca=marca, producto=producto)
            serie = pd.Series(dtype='int64')
            if datos:
                df = pd.DataFrame(datos)
                df = df[df['marca'].astype(str).str.upper() != 'TRAIDO']
                df['cps'] = df['cps'].fillna('Sin CPS')
                serie = df['cps'].value_counts()
        return self._normalizar_serie(serie, 'Sin CPS')

    def get_reporte_cps(self, anio=None, mes=None, asesor=None, cps=None,
                        financiera=None, marca=None, producto=None):
        try:
            hoy = date.today()
            anio = int(anio) if anio is not None else hoy.year
            mes = int(mes) if mes is not None else hoy.month

            if mes == 1:
                mes_ant, anio_ant = 12, anio - 1
            else:
                mes_ant, anio_ant = mes - 1, anio

            serie_ant = self._contar_cps(anio_ant, mes_ant, asesor=asesor, cps=cps,
                                     financiera=financiera, marca=marca, producto=producto)
            serie_act = self._contar_cps(anio, mes, asesor=asesor, cps=cps,
                                         financiera=financiera, marca=marca, producto=producto)

            cps_list = sorted(set(serie_ant.index) | set(serie_act.index),
                              key=lambda x: int(serie_act.get(x, 0)), reverse=True)

            ultimo_dia = calendar.monthrange(anio, mes)[1]
            if (anio, mes) == (hoy.year, hoy.month):
                factor = ultimo_dia / max(hoy.day, 1)
            else:
                factor = 1.0

            rows = []
            suma_ant = suma_act = suma_proy = 0
            for c in cps_list:
                ant = int(serie_ant.get(c, 0))
                act = int(serie_act.get(c, 0))
                proy = int(round(act * factor))
                rows.append({'cps': c, 'mes_anterior': ant, 'cant': act, 'proyeccion': proy})
                suma_ant += ant
                suma_act += act
                suma_proy += proy
            rows.append({'cps': 'TOTAL', 'mes_anterior': suma_ant, 'cant': suma_act, 'proyeccion': suma_proy})

            return {
                "success": True,
                "rows": rows,
                "mes_anterior_label": f"{MESES[mes_ant - 1]} {anio_ant}",
                "mes_actual_label": f"{MESES[mes - 1]} {anio}",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


# 4. Crear y mostrar la ventana de la aplicación
if __name__ == '__main__':
    api = Api()

    window = webview.create_window(
        title='Dashboard Lefcom',
        url='index.html',
        js_api=api,
        width=1000,
        height=700,
        resizable=True
    )

    webview.start()