"""
Motor de cálculo: Reabasto de faltantes con FEFO - CD CCU Puerto Montt.

Entradas (exportes del WMS):
  - OPERACIONES_LINEAS_DE_PEDIDO_SIN_INVENTARIO  -> qué falta (SKU, cajas, pedidos)
  - CUADRATURA_DE_STOCK                           -> stock actual por ubicación/área
  - OPERACIONES_DE_VIDA_UTIL                      -> LPN + fecha de caducidad (FEFO)
Maestros (sacados del ADC PMONTT):
  - maestro_slotting.csv     (pestaña 25: SKU -> ubicación de picking, min, max)
  - maestro_ubicaciones.csv  (pestaña 16: área, zona de movimiento, secuencia de viaje)
"""
import io
import pandas as pd

ZM_PALLET = "ZM-PT01"     # política REAPROPALLET (pallet completo)
ZM_CAJA = "ZM-PT01CJ"     # política REAPROCAJA (por cajas)


# ---------------------------------------------------------------- lectura
def _leer_csv(archivo):
    """Lee CSV del WMS (UTF-8 o latin-1, coma o punto y coma) todo como texto."""
    if hasattr(archivo, "getvalue"):
        raw = archivo.getvalue()
    elif hasattr(archivo, "read"):
        raw = archivo.read()
    else:
        raw = open(archivo, "rb").read()
    for enc in ("utf-8-sig", "latin-1"):
        try:
            txt = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    sep = ";" if txt.splitlines()[0].count(";") > txt.splitlines()[0].count(",") else ","
    df = pd.read_csv(io.StringIO(txt), sep=sep, dtype=str)
    df.columns = [c.strip() for c in df.columns]
    return df


def _sku(s):
    return s.astype(str).str.strip().str.replace(r"\.0$", "", regex=True)


def _num(s):
    return pd.to_numeric(s, errors="coerce").fillna(0)


def cargar_faltantes(archivo):
    df = _leer_csv(archivo)
    df["SKU"] = _sku(df["SKU"])
    df["faltante"] = _num(df["Cantidad de seleccion"])
    df = df[df["faltante"] > 0]
    agg = (df.groupby("SKU")
             .agg(descripcion=("Descripcion", "first"),
                  faltante=("faltante", "sum"),
                  n_pedidos=("Numero de pedido", "nunique"),
                  pedidos=("Numero de pedido", lambda x: ", ".join(sorted(set(x.astype(str))))),
                  tipo_pedido=("Tipo de pedido", lambda x: ", ".join(sorted(set(x.astype(str))))),
                  reabasto_wms=("Reabasto Dirigido", lambda x: "Sí" if x.astype(str).str.startswith("S").any() else "No"))
             .reset_index())
    return agg, df


def cargar_cuadratura(archivo):
    df = _leer_csv(archivo)
    df = df.rename(columns={"ubicación": "ubicacion", "articulo": "SKU"})
    df["SKU"] = _sku(df["SKU"])
    df["cantidad"] = _num(df["cantidad"])
    return df


def cargar_vida_util(archivo):
    df = _leer_csv(archivo)
    df = df.rename(columns={"ubicación": "ubicacion", "Articulo": "SKU", "Area": "area",
                            "Estado de inventario": "estado", "Cantidad": "cantidad",
                            "Fecha de caducidad": "caducidad", "Fecha de fabricacion": "fabricacion"})
    df["SKU"] = _sku(df["SKU"])
    df["cantidad"] = _num(df["cantidad"])
    df["caducidad"] = pd.to_datetime(df["caducidad"], dayfirst=True, errors="coerce")
    df["fabricacion"] = pd.to_datetime(df["fabricacion"], dayfirst=True, errors="coerce")
    return df


# ---------------------------------------------------------------- maestros desde el ADC
def maestros_desde_adc(archivo_adc):
    """Extrae solo lo necesario del ADC (pestañas 16 y 25)."""
    ub = pd.read_excel(archivo_adc, sheet_name="16-Ubicaciones", header=0, dtype=str)
    ub = ub[ub["Ubicacion"].notna()]
    ub = ub.rename(columns={"Ubicacion": "ubicacion", "CÓDIGO DE ÁREA": "area_adc",
                            "Zona de Movimiento": "zona_mov", "Zona de Surtido": "zona_surtido",
                            "SECUENCIA DE VIAJE": "secuencia_viaje", "Pasillo": "pasillo",
                            "SURTIDO": "surtido", "REABASTO": "reabasto",
                            "Zona de Almacenaje": "zona_almacenaje"})
    ub = ub[["ubicacion", "area_adc", "pasillo", "zona_almacenaje", "zona_surtido",
             "zona_mov", "secuencia_viaje", "surtido", "reabasto"]]
    ub["ubicacion"] = ub["ubicacion"].str.strip()

    sl = pd.read_excel(archivo_adc, sheet_name="25-Asign Producto-Ubicación", header=1, dtype=str)
    sl = sl[sl["sku"].notna()].rename(columns={"sku": "SKU", "Ubicación Inicial": "ubicacion_pick",
                                               "min": "min", "max": "max", "UM (CJ/PA)": "um"})
    sl["SKU"] = _sku(sl["SKU"])
    sl = sl[["SKU", "ubicacion_pick", "min", "max", "um"]]
    return ub, sl


# ---------------------------------------------------------------- motor
def calcular(faltantes, cuad, vu, ubic, slot, modo="faltante",
             incluir_stage=False, excluir_reabasto_wms=False):
    """
    modo: 'faltante'  -> mover solo lo que falta
          'maximo'    -> mover lo que falta o hasta completar el máximo del ADC (lo mayor)
    Devuelve: (movimientos, resumen_por_sku)
    """
    ubic = ubic.copy()
    ubic["secuencia_viaje"] = pd.to_numeric(ubic["secuencia_viaje"], errors="coerce")
    slot = slot.drop_duplicates("SKU").copy()
    slot["max"] = _num(slot["max"])
    slot["min"] = _num(slot["min"])

    # Norma de pallet por SKU = cantidad más frecuente de un LPN (proxy de pallet completo)
    vu_d = vu[vu["estado"] == "D"]
    norma = (vu_d[vu_d["cantidad"] > 0].groupby("SKU")["cantidad"]
             .agg(lambda x: x.mode().max()).rename("norma_pallet"))

    # Stock y FEFO actual en picking
    stock_pick = (cuad[(cuad["area"] == "PICK") & (cuad["estado_de_inventario"] == "D")]
                  .groupby("SKU")["cantidad"].sum().rename("stock_pick"))
    venc_pick = (vu_d[vu_d["area"] == "PICK"].groupby("SKU")["caducidad"].min()
                 .rename("venc_pick"))
    ubic_pick_cuad = (cuad[cuad["area"] == "PICK"].groupby("SKU")["ubicacion"].first()
                      .rename("ubic_pick_cuadratura"))

    # Validación cruzada: el LPN debe estar en una ubicación/SKU vigente en la cuadratura
    areas_origen = ["ALMAC"] + (["STAGE"] if incluir_stage else [])
    vigentes = set(zip(cuad.loc[cuad["area"].isin(areas_origen) & (cuad["estado_de_inventario"] == "D"), "ubicacion"],
                       cuad.loc[cuad["area"].isin(areas_origen) & (cuad["estado_de_inventario"] == "D"), "SKU"]))
    origen = vu_d[vu_d["area"].isin(areas_origen)].copy()
    origen = origen[[(u, s) in vigentes for u, s in zip(origen["ubicacion"], origen["SKU"])]]
    origen = origen.merge(ubic[["ubicacion", "secuencia_viaje", "pasillo"]], on="ubicacion", how="left")
    origen = origen.merge(norma, on="SKU", how="left")
    origen["es_resto"] = origen["cantidad"] < origen["norma_pallet"]
    # FEFO: vence antes primero; misma fecha -> restos primero, luego menor cantidad, luego recorrido
    origen = origen.sort_values(["SKU", "caducidad", "es_resto", "cantidad", "secuencia_viaje"],
                                ascending=[True, True, False, True, True])

    base = (faltantes.merge(slot, on="SKU", how="left")
                     .merge(stock_pick, on="SKU", how="left")
                     .merge(venc_pick, on="SKU", how="left")
                     .merge(ubic_pick_cuad, on="SKU", how="left")
                     .merge(norma, on="SKU", how="left"))
    base["stock_pick"] = base["stock_pick"].fillna(0)
    base["en_adc"] = base["ubicacion_pick"].notna()
    base["destino"] = base["ubicacion_pick"].fillna(base["ubic_pick_cuadratura"])
    base = base.merge(ubic[["ubicacion", "zona_mov"]].rename(columns={"ubicacion": "destino"}),
                      on="destino", how="left")
    base["tipo_reapro"] = base["zona_mov"].map({ZM_CAJA: "CAJAS", ZM_PALLET: "PALLET"}).fillna("CAJAS")

    if modo == "maximo":
        espacio = (base["max"] - base["stock_pick"]).clip(lower=0).fillna(0)
        base["objetivo"] = base[["faltante"]].join(espacio.rename("e")).max(axis=1)
    else:
        base["objetivo"] = base["faltante"]

    movs, resumen = [], []
    for _, r in base.iterrows():
        obs = []
        if not r["en_adc"]:
            obs.append("SKU sin slotting en ADC (pestaña 25)")
        if pd.isna(r["destino"]):
            obs.append("Sin ubicación de picking")
        if excluir_reabasto_wms and r["reabasto_wms"] == "Sí":
            resumen.append({**r.to_dict(), "asignado": 0, "pendiente": r["objetivo"],
                            "estado": "OMITIDO (reabasto dirigido WMS)", "observaciones": "; ".join(obs)})
            continue

        pend = r["objetivo"]
        cand = origen[origen["SKU"] == r["SKU"]]
        primer_venc = None
        for _, o in cand.iterrows():
            if pend <= 0:
                break
            if r["tipo_reapro"] == "PALLET":
                mover = o["cantidad"]                  # LPN completo
            else:
                mover = min(o["cantidad"], pend)       # solo las cajas necesarias
            primer_venc = primer_venc or o["caducidad"]
            movs.append({
                "SKU": r["SKU"], "Descripción": r["descripcion"],
                "Origen": o["ubicacion"], "LPN": o["LPN"],
                "Vencimiento": o["caducidad"], "Cajas en LPN": int(o["cantidad"]),
                "Resto": "Sí" if o["es_resto"] else "No",
                "Destino": r["destino"], "Tipo": "Pallet completo" if r["tipo_reapro"] == "PALLET" else "Cajas",
                "Cajas a mover": int(mover), "Faltante SKU": int(r["faltante"]),
                "N° pedidos": int(r["n_pedidos"]), "Pedidos": r["pedidos"],
                "secuencia_viaje": o["secuencia_viaje"],
            })
            pend -= mover

        asignado = r["objetivo"] - max(pend, 0)
        # Alerta FEFO: el picking tiene producto MÁS NUEVO que lo que hay en almacenamiento
        if pd.notna(r["venc_pick"]) and primer_venc is not None and primer_venc < r["venc_pick"]:
            obs.append(f"FEFO: almacenamiento vence antes ({primer_venc:%d/%m/%Y}) que picking ({r['venc_pick']:%d/%m/%Y})")
        if pd.notna(r["max"]) and r["max"] > 0 and asignado > 0 and r["stock_pick"] + asignado > r["max"]:
            obs.append(f"Supera máx ADC ({int(r['max'])}) con stock actual {int(r['stock_pick'])}: revisar espacio")
        if cand.empty and r["stock_pick"] > 0:
            obs.append("Todo el stock D está en picking (posible comprometido/estatus)")
        if cand.empty:
            otros = cuad[(cuad["SKU"] == r["SKU"]) & (cuad["area"] != "PICK")]
            if not otros.empty:
                obs.append("Stock solo en: " + ", ".join(
                    f"{u} ({e}) {int(q)}" for u, e, q in zip(otros["ubicacion"], otros["estado_de_inventario"], otros["cantidad"])))
        estado = "OK" if pend <= 0 else ("PARCIAL" if asignado > 0 else "SIN STOCK EN ALMACENAMIENTO")
        resumen.append({**r.to_dict(), "asignado": asignado, "pendiente": max(pend, 0),
                        "estado": estado, "observaciones": "; ".join(obs)})

    movs = pd.DataFrame(movs)
    if not movs.empty:
        movs = movs.sort_values(["secuencia_viaje", "Origen"]).reset_index(drop=True)
    res = pd.DataFrame(resumen)
    res = res.rename(columns={"descripcion": "Descripción", "faltante": "Faltante", "objetivo": "Objetivo",
                              "stock_pick": "Stock picking", "venc_pick": "Venc. picking",
                              "destino": "Destino", "tipo_reapro": "Tipo", "asignado": "Asignado",
                              "pendiente": "Pendiente", "estado": "Estado", "observaciones": "Observaciones",
                              "n_pedidos": "N° pedidos", "reabasto_wms": "Reabasto dirigido WMS",
                              "norma_pallet": "Norma pallet"})
    cols = ["SKU", "Descripción", "Faltante", "Objetivo", "Asignado", "Pendiente", "Estado", "Destino",
            "Tipo", "Stock picking", "Venc. picking", "Norma pallet", "N° pedidos",
            "Reabasto dirigido WMS", "Observaciones"]
    return movs, res[cols]
