import io
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

import motor

st.set_page_config(page_title="Reabasto Faltantes FEFO · PMONTT", page_icon="🏗️", layout="wide")
DATA = Path(__file__).parent / "data"


@st.cache_data
def maestros_base():
    ub = pd.read_csv(DATA / "maestro_ubicaciones.csv", dtype=str)
    sl = pd.read_csv(DATA / "maestro_slotting.csv", dtype=str)
    return ub, sl


@st.cache_data
def maestros_adc(contenido: bytes):
    return motor.maestros_desde_adc(io.BytesIO(contenido))


# ------------------------------------------------------------------ barra lateral
with st.sidebar:
    st.header("📂 Archivos del WMS")
    f_lineas = st.file_uploader("Líneas de pedido sin inventario", type="csv")
    f_cuad = st.file_uploader("Cuadratura de stock", type="csv")
    f_vu = st.file_uploader("Operaciones de vida útil", type="csv")

    st.header("⚙️ Opciones")
    modo = st.radio("Cantidad a reabastecer",
                    ["Solo el faltante", "Faltante o completar máximo ADC (lo mayor)"])
    incluir_stage = st.checkbox("Usar también stock en recepción (STAGE)", value=False,
                                help="Por defecto solo se toma origen desde área ALMAC.")
    excluir_wms = st.checkbox("Omitir SKUs con 'Reabasto Dirigido = Sí' en el WMS", value=False)

    with st.expander("Maestro ADC"):
        f_adc = st.file_uploader("Subir ADC actualizado (opcional)", type="xlsx",
                                 help="Si no subes nada se usa el maestro guardado en la app (pestañas 16 y 25).")

st.title("🏗️ Reabasto de faltantes con FEFO — CD Puerto Montt")
st.caption("Cruza las líneas sin inventario con el stock de almacenamiento y elige el LPN que vence primero "
           "(restos antes que pallets completos). Destino y tipo de reapro según el ADC.")

if not (f_lineas and f_cuad and f_vu):
    st.info("Sube los 3 archivos del WMS en la barra lateral para generar la hoja de ruta.")
    st.stop()

try:
    ubic, slot = maestros_adc(f_adc.getvalue()) if f_adc else maestros_base()
    faltantes, lineas = motor.cargar_faltantes(f_lineas)
    cuad = motor.cargar_cuadratura(f_cuad)
    vu = motor.cargar_vida_util(f_vu)
    movs, resumen = motor.calcular(
        faltantes, cuad, vu, ubic, slot,
        modo="maximo" if modo.startswith("Faltante o") else "faltante",
        incluir_stage=incluir_stage, excluir_reabasto_wms=excluir_wms)
except KeyError as e:
    st.error(f"❌ Falta la columna {e} en alguno de los archivos. Revisa que sean los exportes correctos del WMS.")
    st.stop()
except Exception as e:
    st.error(f"❌ Ocurrió un error al procesar los datos: {e}")
    st.stop()

# ------------------------------------------------------------------ indicadores
alertas_fefo = resumen["Observaciones"].str.contains("FEFO", na=False).sum()
sin_stock = (resumen["Estado"] != "OK").sum()
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("SKUs con faltante", len(resumen))
c2.metric("Movimientos", len(movs))
c3.metric("Pallets completos", int((movs["Tipo"] == "Pallet completo").sum()) if not movs.empty else 0)
c4.metric("Alertas FEFO", int(alertas_fefo))
c5.metric("Sin cubrir / parcial", int(sin_stock))

tab_ruta, tab_resumen, tab_lineas = st.tabs(["🖨️ Hoja de ruta", "📋 Resumen por SKU", "📄 Líneas originales"])

# ------------------------------------------------------------------ resumen
with tab_resumen:
    def color_estado(v):
        return {"OK": "background-color:#DCFCE7", "PARCIAL": "background-color:#FEF9C3"}.get(
            v, "background-color:#FEE2E2" if isinstance(v, str) and v else "")
    vista = resumen.copy()
    vista["Venc. picking"] = pd.to_datetime(vista["Venc. picking"]).dt.strftime("%d-%m-%Y")
    st.dataframe(vista.style.map(color_estado, subset=["Estado"]), use_container_width=True, hide_index=True)

with tab_lineas:
    st.dataframe(lineas, use_container_width=True, hide_index=True)

# ------------------------------------------------------------------ hoja de ruta
with tab_ruta:
    if movs.empty:
        st.warning("No hay movimientos posibles con el stock de almacenamiento actual.")
    else:
        fecha_hora = datetime.now().strftime("%d-%m-%Y %H:%M")
        df_final = movs.copy()
        df_final["Vencimiento"] = pd.to_datetime(df_final["Vencimiento"]).dt.strftime("%d-%m-%Y")

        # Descarga Excel (hoja de ruta + resumen)
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as xw:
            df_final.drop(columns=["secuencia_viaje"]).to_excel(xw, sheet_name="Hoja de ruta", index=False)
            vista.to_excel(xw, sheet_name="Resumen SKU", index=False)
        st.download_button("⬇️ Descargar Excel", buf.getvalue(),
                           file_name=f"reabasto_fefo_pmontt_{datetime.now():%Y%m%d_%H%M}.xlsx")

        filas_html = ""
        for idx, r in df_final.iterrows():
            tipo_color = "#7C2D12" if r["Tipo"] == "Pallet completo" else "#1E40AF"
            resto = " <span style='font-size:11px;color:#B45309;'>(resto)</span>" if r["Resto"] == "Sí" else ""
            filas_html += f"""
            <tr>
                <td style="text-align:center;">{idx + 1}</td>
                <td style="font-weight:bold;font-family:monospace;font-size:14px;">{r['SKU']}</td>
                <td>{r['Descripción']}</td>
                <td style="text-align:center;font-weight:bold;color:#1E3A8A;font-size:15px;">{r['Origen']}</td>
                <td style="text-align:center;font-family:monospace;font-size:11px;">{r['LPN']}{resto}</td>
                <td style="text-align:center;font-weight:bold;">{r['Vencimiento']}</td>
                <td style="text-align:center;font-weight:bold;color:#065F46;font-size:15px;">{r['Destino']}</td>
                <td style="text-align:center;font-weight:bold;color:{tipo_color};">{r['Tipo']}</td>
                <td style="text-align:center;font-weight:bold;font-size:16px;">{r['Cajas a mover']}</td>
                <td style="width:70px;"></td>
            </tr>"""

        html_report = f"""
        <style>
            @media print {{
                body {{ font-family: Arial, sans-serif; margin: 0; padding: 0; }}
                .no-print {{ display: none !important; }}
                @page {{ size: landscape; margin: 10mm; }}
            }}
            .report-card {{ border:2px solid #1E293B; border-radius:8px; padding:20px; background:#FFF;
                            color:#0F172A; font-family:Arial, sans-serif; }}
            .header-table {{ width:100%; border-collapse:collapse; margin-bottom:15px; }}
            .header-table td {{ padding:4px; }}
            .title {{ font-size:22px; font-weight:bold; text-transform:uppercase; letter-spacing:1px; }}
            .data-table {{ width:100%; border-collapse:collapse; margin-top:15px; }}
            .data-table th {{ background:#1E293B; color:white; border:1px solid #1E293B; padding:8px 6px;
                              font-size:12px; text-transform:uppercase; }}
            .data-table td {{ border:1px solid #94A3B8; padding:8px 6px; font-size:13px; }}
            .data-table tr {{ page-break-inside: avoid; }}
        </style>
        <div class="report-card">
            <table class="header-table">
                <tr>
                    <td class="title">Reabasto FEFO / Hoja de Ruta — CD Puerto Montt</td>
                    <td style="text-align:right;font-size:12px;"><strong>Fecha Emisión:</strong> {fecha_hora}</td>
                </tr>
                <tr><td colspan="2" style="font-size:13px;color:#475569;">
                    <strong>SKUs:</strong> {df_final['SKU'].nunique()} &nbsp;|&nbsp;
                    <strong>Movimientos:</strong> {len(df_final)} &nbsp;|&nbsp;
                    <strong>Pallets completos:</strong> {(df_final['Tipo'] == 'Pallet completo').sum()} &nbsp;|&nbsp;
                    Ordenado por secuencia de viaje del origen. Mover exactamente el LPN indicado (FEFO).
                </td></tr>
            </table>
            <table class="data-table">
                <thead><tr>
                    <th>#</th><th>SKU</th><th>Descripción</th><th>Origen</th><th>LPN</th><th>Vence</th>
                    <th>Destino</th><th>Tipo</th><th>Cajas</th><th>Check (✓)</th>
                </tr></thead>
                <tbody>{filas_html}</tbody>
            </table>
        </div>"""

        st.components.v1.html(
            f"""{html_report}
            <div style="margin-top:15px;text-align:center;" class="no-print">
                <button onclick="window.print()" style="background:#2563EB;color:white;font-weight:bold;
                    padding:12px 24px;font-size:16px;border:none;border-radius:6px;cursor:pointer;">
                    🖨️ Imprimir Hoja de Ruta
                </button>
            </div>""",
            height=800, scrolling=True)
