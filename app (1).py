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
import gzip
import base64
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


# =====================================================================================
#  MAESTROS ADC EMBEBIDOS (no requiere carpeta data/)
# =====================================================================================
# Maestro extraído del ADC PMONTT (maestro_ubicaciones.csv), comprimido.
MAESTRO_UBICACIONES = (
    "H4sIAH3OxmoC/6Wd3ZIkR3Kd7/UsA7MM9xMRmZeDwQCY3cWPAHBNxhsZuNyLlVFcmSjqQk+vGUx7RXp0LLyOh0wyUuj+xtM9IzLP"
    "V5VV/Z//8re//PqXv/3939/8+r//+ut///Vf//Lmf/36H3/7t3/7+5v/9/d///gf/u1//vqXv/77r//jr5/////xn//7//ztX19+"
    "+D///n/f/Mdf//Kff/33v/zt1//+f//26bfsFz7+a//y63/8n7//lx/LF+V48/ZP37199+bH8uaf337x4/uf3n379ud3b/755y8+"
    "//d//u6LH385yptyHG/Km+M3pjzLlMFIgtFnGRkMEkx9ltHBtATTn2UwmDPBXM8y9cHIkWCeXgdtMJJgnl4HfTBIME+vg3MwLcE8"
    "vQ6uwZwJ5tl1IGNv65Fgnl0HMva2SoJ5dh3I2NuKBPPsOpCxt7UlmGfXgdjeli+OxzqQG/On10w9BnM9y5QHM+4LESODKc8yOhh5"
    "lhkzGPeFiKmDwbNMG0x9lumDac8y52D6s8w1mGfXQRvroDy7DtpYB/LsOmhjHciz66CNdSDProM21oE8uw7aWAfy7DpoYx3Is+ug"
    "jXUgz66DNtaBPLsO2lgH8uw66GMdyLProI91oM+ugz7WgT67DvpYB/rsOuhjHeiz66CPdaDProM+1oE+uw76WAf67DroYx3os+ug"
    "v6yDj9m8vPn5l7ffvH/z8f/cEvvHH37+sSx/LPZjXf5Y7cdY/hj247r8cbUft+WPm/24L3/c7cfn8sen/fha/vh6+fHH9bv48cf/"
    "+vLj5dSKTa0sp1ZsamU5tWJTK8upFZtaWU6t2NTKcmrFplaWUys2tbKcWrGpleXUik1NllMTm5ospyY2NVlOTWxqspya2NRkOTWx"
    "qclyamJTk+XUxKYmy6mJTU2WUxObmiynJjY1XU5NbWq6nJra1HQ5NbWp6XJqalPT5dTUpqbLqalNTZdTU5uaLqemNjVdTk1tarqc"
    "mtrUsJwabGpYTg02NSynBpsallODTQ3LqcGmhuXUYFPDcmqwqWE5NdjUsJwabGpYTg02tbqcWrWp1eXUqk2tLqdWbWp1ObVqU6vL"
    "qVWbWl1OrdrU6nJq1aZWl1OrNrW6nFq1qdXl1KpNrS2n1mxqbTm1ZlNry6k1m1pbTq3Z1Npyas2m1pZTaza1tpxas6m15dSaTa0t"
    "p9Zsam05tWZT68updZtaX06t29T6cmrdptaXU+s2tb6cWrep9eXUuk2tL6fWbWp9ObVuU+vLqXWbWl9OrdvUzuXUTpvauZzaaVM7"
    "l1M7bWrncmqnTe1cTu20qZ3LqZ02tXM5tdOmdi6ndtrUzuXUTpvauZzaaVO7llO7bGrXcmqXTe1aTu2yqV3LqV02tWs5tcumdi2n"
    "dtnUruXULpvatZzaZVO7llO7bGrXcmrXy9Q+vTL/+sef/uunH8vSDcTcQJZuIOYGsnQDMTeQpRuIuYEs3UDMDWTpBmJuIEs3EHMD"
    "WbqBmBvI0g3E3ECWbiDmBrJ0AzE3kKUbiLmBLN1AzA1k6QZibiBLNxBzA1m6gZgbyNINxNxAlm4g5gaydAMxN5ClG4i5gSzdQMwN"
    "ZOkGYm4gSzcQcwNZuoGYG8jSDcTcQJZuIOYGsnQDMTeQpRuIuYEs3UDMDWTpBmJuIEs3EHMDWbqBmBvI0g3E3ECWbiDmBrJ0AzE3"
    "kKUbiLmBLN1AzA1k6QZibiBLNxBzA1m6gZgbyNINxNxAlm4g5gaydAMxN5ClG4i5gSzdQMwNZOkGYm4gSzcQcwNZuoGYG8jSDcTc"
    "QJZuIOYGsnQDMTeQpRuIuYEs3UDMDWTpBmJuIEs3EHMDWbqBmBvI0g3E3ECWbiDmBrJ0AzE3kKUbiLmBLN1AzA1k6QZibiBLNxBz"
    "A1m6gZgbyNINxNxAlm4g5gaydAMxN5ClG4i5gSzdQMwNZOkGYm4gSzcQcwNZuoGYG8jSDcTcQJZuIOYGsnQDMTeQpRuIuYEs3UDM"
    "DWTpBmJuIEs3EHMDWbqBmBvI0g3E3ECWbiDmBrJ0AzE3kKUbiLmBLN1AzA1k6QZibiBLNxBzA1m6gZgbyNINxNxAlm4g5gaydAMx"
    "N5ClG4i5gSzdQMwNZOkGYm4gSzcQcwNZuoGYG8jSDcTcQJZuIOYGsnQDMTeQpRuIuYEu3UDNDXTpBmpuoEs3UHMDXbqBmhvo0g3U"
    "3ECXbqDmBrp0AzU30KUbqLmBLt1AzQ106QZqbqBLN1BzA126gZob6NIN1NxAl26g5ga6dAM1N9ClG6i5gS7dQM0NdOkGam6gSzdQ"
    "cwNduoGaG+jSDdTcQJduoOYGunQDNTfQpRuouYEu3UDNDXTpBmpuoEs3UHMDXbqBmhvo0g3U3ECXbqDmBrp0AzU30KUbqLmBLt1A"
    "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"
    "FOFMEc4U4UwRzrSGM63hTGs40xrOtIYzreFMazjTGs60hjOt4UxbONMWzrSFM23hTFs40xbOtIUzbeFMWzjTFs60hzPt4Ux7ONMe"
    "zrSHM+3hTHs40x7OtIcz7eFMz3CmZzjTM5zpGc70DGd6hjM9w5me4UzPcKZnONMrnOkVzvQKZ3qFM73CmV7hTK9wplc40yuc6RXN"
    "9JMw/e5vSOhREnqUhB4loUdJ6FESepSEHiWhR0noURJ6lIQeJaFHSehREnqUhB4loUdJ6FESepSEHiWhR0noURJ6lIQeJaFHSehR"
    "EnqUhB4loUdJ6FESepSEHiWhR0noURJ6lIQeJaFHSehREnqUhB4loUdJ6FESepSEHiWhR0noURJ6lIQeJaFHSehREnqUhB4loUdJ"
    "6FESepSEHiWhR0noURJ6lIQeJaFHSehREnqUhB4loUdJ6FESepSEHiWhR0noURJ6lIQeJaFHSehREnqUhB4loUdJ6FESepSEHiWh"
    "R0noURJ6lIQeJaFHSehREnqUhB4loUdJ6FESepSEHiWhR0noURJ6lIQeJaFHSehREnqUhB4loUdp6FEaepSGHqWhR2noURp6lIYe"
    "paFHaehRGnqUhh6loUdp6FEaepSGHqWhR2noURp6lIYepaFHaehRGnqUhh6loUdp6FEaepSGHqWhR2noURp6lIYepaFHaehRGnqU"
    "hh6loUdp6FEaepSGHqWhR2noURp6lIYepaFHaehRGnqUhh6loUdp6FEaepSGHqWhR2noURp6lIYepaFHaehRGnqUhh6loUdp6FEa"
    "epSGHqWhR2noURp6lIYepaFHaehRGnqUhh6loUdp6FEaepSGHqWhR2noURp6lIYepaFHaehRGnqUhh6loUdp6FEaepSGHqWhR2no"
    "URp6lIYepaFHaehRGnqUhh6loUdp6FEaepSGHqWhRyH0KIQehdCjEHoUQo9C6FEIPQqhRyH0KIQehdCjEHoUQo9C6FEIPQqhRyH0"
    "KIQehdCjEHoUQo9C6FEIPQqhRyH0KIQehdCjEHoUQo9C6FEIPQqhRyH0KIQehdCjEHoUQo9C6FEIPQqhRyH0KIQehdCjEHoUQo9C"
    "6FEIPQqhRyH0KIQehdCjEHoUQo9C6FEIPQqhRyH0KIQehdCjEHoUQo9C6FEIPQqhRyH0KIQehdCjEHoUQo9C6FEIPQqhRyH0KIQe"
    "hdCjEHoUQo9C6FEIPQqhRyH0KIQehdCjEHoUQo9C6FEIPQqhRyH0KIQehdCjEHoUQo9C6FEIPQqhRyH0KIQehdCjEHpUDT2qhh5V"
    "Q4+qoUfV0KNq6FE19KgaelQNPaqGHlVDj6qhR9XQo2roUTX0qBp6VA09qoYeVUOPqqFH1dCjauhRNfSoGnpUDT2qhh5VQ4+qoUfV"
    "0KNq6FE19KgaelQNPaqGHlVDj6qhR9XQo2roUTX0qBp6VA09qoYeVUOPqqFH1dCjauhRNfSoGnpUDT2qhh5VQ4+qoUfV0KNq6FE1"
    "9KgaelQNPaqGHlVDj6qhR9XQo2roUTX0qBp6VA09qoYeVUOPqqFH1dCjauhRNfSoGnpUDT2qhh5VQ4+qoUfV0KNq6FE19KgaelQN"
    "PaqGHlVDj6qhR9XQo2roUTX0qBp6VA09qoYeVUOPqqFH1dCjauhRNfSoGnpUDT2qhh5VQ4+qoUe10KNa6FEt9KgWelQLPaqFHtVC"
    "j2qhR7XQo1roUS30qBZ6VAs9qoUe1UKPaqFHtdCjWuhRLfSoFnpUCz2qhR7VQo9qoUe10KNa6FEt9KgWelQLPaqFHtVCj2qhR7XQ"
    "o1roUS30qBZ6VAs9qoUe1UKPaqFHtdCjWuhRLfSoFnpUCz2qhR7VQo9qoUe10KNa6FEt9KgWelQLPaqFHtVCj2qhR7XQo1roUS30"
    "qBZ6VAs9qoUe1UKPaqFHtdCjWuhRLfSoFnpUCz2qhR7VQo9qoUe10KNa6FEt9KgWelQLPaqFHtVCj2qhR7XQo1roUS30qBZ6VAs9"
    "qoUe1UKPaqFHtdCjWuhRLfSoFnpUCz2qhR7VQo9qoUe10KNa6FEt9KgWelQPPaqHHtVDj+qhR/XQo3roUT30qB56VA89qoce1UOP"
    "6qFH9dCjeuhRPfSoHnpUDz2qhx7VQ4/qoUf10KN66FE99KgeelQPPaqHHtVDj+qhR/XQo3roUT30qB56VA89qoce1UOP6qFH9dCj"
    "euhRPfSoHnpUDz2qhx7VQ4/qoUf10KN66FE99KgeelQPPaqHHtVDj+qhR/XQo3roUT30qB56VA89qoce1UOP6qFH9dCjeuhRPfSo"
    "HnpUDz2qhx7VQ4/qoUf10KN66FE99KgeelQPPaqHHtVDj+qhR/XQo3roUT30qB56VA89qoce1UOP6qFH9dCjeuhRPfSoHnpUDz2q"
    "hx7VQ4/qoUf10KN66FE99KgeelQPPaqHHtVDj+qBR/35/ffvPnz1w5s//fDNh3dvP/7G2y8+/q/f//Duh+9/tt9vb9qnh/8+f1H6"
    "d+9/+u7t9Osff9l+t3732y+//BWcn96//+7Ht//1n97/DvDTb4Aa8MvXf3r/S/nHv4/ffh2PX3//fXT4129ENeLPH37+8PGf+x3i"
    "owN9Il7+4sW7H7786YcP//S7JfpvwMsfe/jx7Z8+tvAPfv+3XzztX/7+53/67ocnDuXlq/2/+/Dffvnh5zdv//Td23e//frH/3D7"
    "O1efnjd8+Tb74ws9vjjKx//75scP7/745u3x6dd/+1//+eff/uc3P73901gPx/Eb+/F/+y9vf3r/bmXWn/75l9P626/I+lf09iu6"
    "/hXcfgXrX6m3X6nrX3k5PT//8vl4X//Zr08PJN5/R9a/c95/R9e/c91/B8vfkeP+O3X9O+VxeuRiT095OT2fz6t8Yj8vhM/wp6Xw"
    "kf38n25g6f0T+Lno+XzRd3/4XFYeZaXztA668TQGXT93zdB1pqnabdD4RJeToftMd4Y+3anW+VS//fQ/Vqf6rI9T/QlEBnw53sYc"
    "7zV3WwlajtGtUAu7zAcNpmyZD1oZWmaaWZqiM80sTYEb2PPLo7exPIRZHjfw5XgP5njr1O3HOzJBj434qdXnl0eT6aAPZgfLvIMP"
    "ZgfLOdPMfpJ5Px3MftJjppltofO2OJhtofO2oK7Yqu5UEwv7GAtbqYV9zGuE2Yh6uz99OtgvhKLHtvh0xM8v7HrbjZ/LMrtR23TQ"
    "hdmN2mea2VR6zjSzqfRyA3t+ebRxo/gEIgO+HC+zjXHM3TLbGLe8V5nlgWs+aGb/Q+aDZvY/dKaZ/Y95P1G3RdSZZrYFmhv384ur"
    "3jJXZRbXDfx8vNRtEfNGpO5uuCXMRi0uzAfN7GBc80Ez+6nO+4m6LdbiWn7+BH+e1WEgMuDL8TIbsc4bkboR19uttFMneKSmTyAx"
    "JnUgMuBLo8w1o87XDOoOXm/34JMa0y1xnNSYigORAUX4vXO76wu/d8bF5riYMelt71zMmPRyIDLgS6PUpjvnMVGbblzeykGNaSS6"
    "clBj6g5EBnxplNl0bVyJC/ValI57XqFe2ZhAZMCXRpnrQ7vZUKE3XRuX8EK9sqG3vV7o3dp0PmhmCzS4gyZO0YgGhXp14Qa+HC+z"
    "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"
    "kZEOLiO1w1XVNIkcyWakNjLSwWWkVlxVTZNIkDJ3KnGnlpHkVacSVBVXVdMkcuTJdqqj05PrVF1VTZPIkRfbKUanF9cpXFVNk0iR"
    "013miU7ro9P5LhNVra6qpknkyMJ22kanheu0uaqaJpEjhe20j06F67S7qpomkSOV7fQcnSrX6emqappEjgTb6TU6Bdfp5apqmkSO"
    "rGSnj0/XyysTD6o+/pScvDJxjkSObGynZXTauE6Lq6ppEjmSzUh9ZKTCZaQurqqmSeRINiP1kZEKl5G6uqqaJpEj2YzUR0YqXEbq"
    "cFU1TSJFCpuR+shIwmWkXl1VTZPIkWxG6iMjCZeRenNVNU0iR7IZqY+MJFxG6t1V1TSJHMlmpD4yknAZqZ+uqqZJ5Eg2I/WRkYTL"
    "SP1yVTVNIkeyGekcGUm4jHQerqqmSeRINiOdIyMJl5HO4qpqmkSOZDPSOTKScBnpFFdV0yRyJJuRzpGRhMtIp7qqmiaRI9mMdI6M"
    "JFxGOuGqappEilQ2I50jIymXkc7qqmqaRI5kM9I5MpJyGelsrqqmSeRINiOdIyMpl5HO7qpqmkSOZDPSOTKSchnpPF1VTZPIkWxG"
    "OkdGUi4jnZerqmkSOZLNSNfISMplpOtwVTVNIkeyGekaGUm5jHQVV1XTJHIkm5GukZGUy0iXuKqaJpEj2Yx0jYykXEa61FXVNIkc"
    "yWaka2Qk5TLSBVdV0yRSJNiMdI2MBC4jXdVV1TQJnvxy7vTLJzp9yUhfvur0y6hqc1U1TSJF+tX7TKf90em0esOq3VXVNIkcebKd"
    "nqPTk+v0dFU1TSJBzt9y8GX87Ir9ZcEvX33LwZfBcyTX5apqmkSKnFZv2OnjryB++epbDoKqL+SjqqZJ5MiT7bSMTk+u0+KqappE"
    "juxspzI67Vyn4qpqmkSObGynOjptXKfqqmqaRI6sbKcYnVauU7iqmiaRI8F2Wken4DqtrqqmSeRIZTtto1PlOm2uqqZJ5EhhO+2j"
    "U+E67a6qpknkyMJ2eo5OC9fp6apqmkSOPNhOR0bSg+v0clU1TSJFCpuRyshIwmWkcriqmiaRI9mMVEZGEi4jleKqappEjmQzUhkZ"
    "SbiMVMRV1TSJHMlmpDIyknAZqairqmkSOZLNSGVkJOEyUoGrqmkSOZLNSGVkJOEyUqmuqqZJ5Eg2I5WRkYTLSKW5qpomkSPZjFRG"
    "RhIuI5XuqmqaRI5kM1IZGUm4jFROV1XTJHIkm5HKyEjCZaRyuaqaJpEiC5uRZGSkwmUkOVxVTZPIkWxGkpGRCpeRpLiqmiaRI9mM"
    "JCMjFS4jibiqmiaRI9mMJCMjFS4jibqqmiaRI9mMJCMjFS4jCVxVTZPIkWxGkpGRCpeRpLqqmiaRI9mMJCMjFS4jSXNVNU0iR7IZ"
    "SUZGKlxGku6qappEjmQzkoyMVLiMJKerqmkSOZLNSDIyUuEyklyuqqZJpMiDzUg6MtLBZSQ9XFVNk8iRbEbSkZEOLiNpcVU1TSJH"
    "shlJR0Y6uIyk4qpqmkSCnL/lIHj3X29/bfDLV99y8PJO/J/f/vS6qt7+8t+Xr77lgCSRI0+20zI6PblOi6uqaRI58mI7ldHpxXUq"
    "rqqmSaTIcrCd6qPTcnCdqquqaRI5srCdYnRauE7hqmqaRI4UttM6OhWu0+qqappEjlS20/E03Zzwo6rNVdU0iRwJttPxNN1sbVHV"
    "7qpqmkSOrGyn42m62cSjqqerqmkSObKxnV6j08Z1ermqmiaRI9mMJCMjFS4jyeGqappEjmQzkoyMVLiMJMVV1TSJHMlmJBkZqXAZ"
    "ScRV1TSJFClsRpKRkYTLSKKuqqZJ5Eg2I8nISMJlJIGrqmkSOZLNSDIyknAZSaqrqmkSOZLNSDIyknAZSZqrqmkSOZLNSDIyknAZ"
    "SbqrqmkSOZLNSDIyknAZSU5XVdMkciSbkWRkJOEyklyuqqZJ5Eg2I+nISMJlJD1cVU2TyJFsRtKRkYTLSFpcVU2TyJFsRtKRkYTL"
    "SLdXJIXLSBOJFKlsRtKRkZTLSKquqqZJ5Eg2I+nISMplJIWrqmkSOZLNSDoyknIZSaurqmkSOZLNSDoyknIZSZurqmkSOZLNSHr7"
    "VCaXkbS7qpomkSPZjKS3T2VyGUlPV1XTJNLkpwvL852OjKRsvsLhWGJKGAlAuXw1kUiT1JQwUoey2QziWGZKt3sql80mEmmSm9LL"
    "ffzd49PBL+y7333P990fPtNw9G1On/HfOeaX+9S7+VPJz9Wtrq4SdaurixT58ulialLtccTT54vDum2uS02qu7rMpLqrixx58pM6"
    "xxGf3KTOuS41qcvVZSZ1ubrIkZ2elP2dsXevPt8c1bW/+fVu/lT1c3WLq0tMyv4C17tXn45myPb8EX9mZRxv4+YkrqqmSeTIynY6"
    "ruZTBgqrqquqaRI5Emyn484z5dqwKlxVTZPIkcp2Ou51k6uEVaurqmkSOVLYTm/3SOE6ba6qpknkyMJ2ersrF67T7qpqmkSOPNhO"
    "b3dzLiHW01XVNIkUKRfb6cgPwiW8ermqmiaRI0+y0zZyh3AJrR2uqqZJ5MjOdjqSjnAJqxVXVdMkciSbkdrISMJlpCauqqZJ5Eg2"
    "I7WRkYTLSE1dVU2TyJFsRmojIwmXkRpcVU2TyJFsRmojIwmXkVp1VTVNIkeyGamNjCRcRmrNVdU0iRzJZqQ2MpJwGal1V1XTJHIk"
    "m5HayEjCZaR2uqqaJpEiC5uR2shIhctI7XJVNU0iR7IZqY+MVLiM1A9XVdMkciSbkfrISIXLSL24qpomkSPZjNRHRipcRuriqmqa"
    "RI5kM1IfGalwGamrq6ppEjmSzUh9ZKTCZaQOV1XTJHIkm5H6yEiFy0i9uqqaJpEj2YzUR0YqXEbqzVXVNIkcyWakPjJS4TJS766q"
    "pknkSDYj9ZGRCpeR+umqappEijzYjNRHRjq4jNQvV1XTJHIkm5HOkZEOLiOdh6uqaRI5ks1I58hIB5eRzuKqappEgpw/Hf0ufp7A"
    "/nbXu1efjn4XPMVgf0fr3atPR5MkcuTJdqqj05PrVF1VTZPIkRfbKUanF9cpXFVNk0iR013miU7ro9P5LhNVra6qpknkyMJ22kan"
    "heu0uaqaJpEjhe20j06F67S7qpomkSOV7fQcnSrX6emqappEjgTb6TU6Bdfp5apqmkSOrGSn9re73r36dHRU1f6O1rtXn44mSeTI"
    "xnZaRqeN67S4qpomkSPZjHSNjFS4jHSJq6ppEjmSzUjXyEiFy0iXuqqaJpEj2Yx0jYxUuIx0wVXVNIkUKWxGukZGEi4jXdVV1TSJ"
    "HMlmpGtkJOEy0tVcVU2TyJFsRrpGRhIuI13dVdU0iRxJZKSXp1evkZKES0nX6epqmkSavD0h/FSvIycJ2Ek9/ibWu1efrw6O+vH3"
    "qd69+nw1SSJNcpN6/FWsd/Ons+Pd8/g7U+9efTo7POZxb5XKzcmTyJGN7XTcz6VxnaqrqmkSObKznY77uXSuU7iqmiaRI0+209v9"
    "/OQ6ra6qpknkyIvt9HY/v7hOm6uqaRIpUg+203E/14PrtLuqmiaRIwvb6biba+E6PV1VTZPIkcLfj8fdXIXr9XJ1NU0iTZL34zKy"
    "h9IZ7/H3m969+nx3dNRleL1SGW8mkSbZSY30oXzGK+poZlLj7qpcxptIpEl2UuOerpWfVHU0M6lxz1Iu5U0k0iQ7qTZPShn6dvdh"
    "E2Y5HctM+XZt5RLmRCJHsgmz3K7mXMIsl6uqaRI5kk2YcruWcwlTDldV0yRyJJsw5Xbf4RKmFFdV0yRSJNiEKeOuAy5hiriqmibB"
    "k1/N3zbwVYk7fblDfvXqOw6+KkFVdVU1TSJF+tX7TKd4dDqt3rAqXFVNk8iRJ9tpHZ2eXKfVVdU0iRzZ2U7b6LRznTZXVdMkcmRj"
    "O+2j08Z12l1VTZPIkZXt9BydVq7T01XVNIkcCbbTa3QKrtPLVdU0iRypZKf2bZBfvfqUf1TVvpnxq1ef8idJ5EhhOy2jU+E6La6q"
    "pknkyMJ2KqPTwnUqrqqmSeRINiPpyEjKZSRVV1XTJFKksBlJR0YSLiMpXFVNk8iRbEbSkZGEy0haXVVNk8iRbEbSkZGEy0jaXFVN"
    "k8iRbEbSkZGEy0jaXVVNk8iRbEbSkZGEy0h6uqqaJpEj2YykIyMJl5H0clU1TSJHshkJIyMJl5FwuKqaJpEj2YyEkZGEy0gorqqm"
    "SeRINiNhZCThMhLEVdU0iRzJZiSMjCRcRoK6qpomkSILm5EwMlLhMhLgqmqaRI5kMxJGRipcRkJ1VTVNIkeyGQkjIxUuI6G5qpom"
    "kSPZjISRkQqXkdBdVU2TyJFsRsLISIXLSDhdVU2TyJFsRsLISIXLSLhcVU2TyJFsRqojIxUuI9XDVdU0iRzJZqQ6MlLhMlItrqqm"
    "SeRINiPVkZEKl5GquKqaJpEj2YxUR0YqXEaq6qpqmkSKPNiMVEdGOriMVOGqappEjmQzUh0Z6eAyUq2uqqZJ5Eg2I9WRkQ4uI9Xm"
    "qmqaRIKcP+X/Vfzuv31/7VevPuX/VfBOvH2X7FevPuVPksiRJ9vpOTo9uU5PV1XTJHLkxXZ6jU4vrtPLVdU0iRQ53WXiTu37a796"
    "9Sn/qKp9l+xXrz7lT5LIkYXttIxOC9dpcVU1TSJHCtupjE6F61RcVU2TyJH6+53+/N0fPaqjUeUaVVdU0yRyJMhGMRoF1yhcUU2T"
    "yJGVbLSORivXaHVFNU0iRzay0TYabVyjzRXVNIkc2clGRzwqXDxq3RXVNIkceZKNjnRUuHTUTldU0yRy5EU2OsJR4cJRu1xRTZNI"
    "kXJwjfaRjYTLRv1wRTVNIkcWstERjYSLRr24opomkSOFbHQkI+GSURdXVNMkciSZjPpIRsIlo66uqKZJ5EgyGfWRjIRLRh2uqKZJ"
    "5EgyGfWRjIRLRr26opomkSPJZNRHMhIuGfXmimqaRI4kk1EfyUi4ZNS7K6ppEjmSTEZ9JCPhklE/XVFNk8iRZDLqIxkJl4z65Ypq"
    "mkSKVDIZnSMZKZeMzsMV1TSJHEkmo3MkI+WS0VlcUU2TyJFkMjpHMlIuGZ3iimqaRI4kk9E5kpFyyehUV1TTJHIkmYzOkYyUS0Yn"
    "XFFNk8iRZDI6RzJSLhmd1RXVNIkcSSajcyQj5ZLR2VxRTZPIkWQyOkcyUi4Znd0V1TSJBPnhi+N8afFD+CK9fUHtJ6o/T12Dak9T"
    "9sWpn6j6PFUGhecpGZQ+T+mg5HkKgyrPUy/Xi/fzG/jvf/9tbRy/fbPanb2tzPcvbzF/+P6Hn1+vk8EexuoGiyx7sf2W0e/F9ltc"
    "Xd1gkWT926HP9CuPfqe3Q5+oK66ubrDIsoXtV0e/he1XXV3dYJFlhe0Xo19h+4Wrqxsssqyy/Y5r3fQO6RN1q6urGyyyLNh+2+gX"
    "bL/N1dUNFlm2sv320W9l++2urm6wyLKN7fcc/Ta239PV1Q0WWbaz/V6j3872e7m6usEiy7L5Ska+Kmy+ksPV1Q0WWZbNVzLyVWHz"
    "lRRXVzdYJFlh85WMfCVsvhJxdXWDRZZl85WMfCVsvhJ1dXWDRZZl85WMfCVsvhK4urrBIsuy+UpGvhI2X0l1dXWDRZZl85WMfCVs"
    "vpLm6uoGiyzL5isZ+UrYfCXd1dUNFlmWzVcy8pWw+UpOV1c3WGRZNl/JyFfC5iu5XF3dYJFl2XylI18Jm6/0cHV1g0WWZfOVjnwl"
    "bL7S4urqBoskq2y+0pGvlM1XKq6ubrDIsmy+0pGvlM1Xqq6ubrDIsmy+0pGvlM1XCldXN1hkWTZf6chXyuYrra6ubrDIsmy+0pGv"
    "lM1X2lxd3WCRZYl89e4Pn+mRsJRNWNpdZd1gscGOLxR/ruORsbTx87oczc1rJAdlE9rEYoMl54WRWbTT80JxNDUv3O7EbMKbWGyw"
    "7Lxknpcy9O2OevLThqO5ad/uE2y+nFhssOy0b3eoi59XczQ3r9t1l82nE4sNlp3XuN7j4Od1Opqb1/BEsPl2YrHBsvMaz5R6+it5"
    "gq6HoxdP0vzj467jsUkQHyu2ysVVVqpycZWRZP3qfO6ob49BXuy8ZK5MzUtdZW5e6iojyzJfCPAbe3vE8GSnBVdXN1hk2J9Gpz/9"
    "fi6vt+d+fhrr6RmqDEqfp2RQeJ7SQdXnKQyqPU/ZPW++MryXmG2OvV/BP5+7n97/8vrM1fv75K+uKCSJFDnd3Z/otD86ne/tUdXu"
    "qmqaRI482U7P0enJdXq6qpomkSM72+k1Ou1cp5erqmkSObKRnT7eiX/1sHFU9fF++KuHjUkSObKynZbRaeU6La6qpknkSLCdyugU"
    "XKfiqmqaRI5UtlMdnSrXqbqqmiaRI4XtFKNT4TqFq6ppEjmysJ2O1DG/1h9Vra6qpknkSDYjychIymUkaa6qpkmkSGEzkoyMJFxG"
    "ku6qappEjmQzkoyMJFxGktNV1TSJHMlmJBkZSbiMJJerqmkSOZLNSDoyknAZSQ9XVdMkciSbkXRkJOEykhZXVdMkciSbkXRkJOEy"
    "koqrqmkSOZLNSDoyknAZSdVV1TSJHMlmJB0ZSbiMpHBVNU0iR7IZSUdGEi4jaXVVNU0iR7IZSUdGEi4jaXNVNU0iRRb/nkn02nC9"
    "v0f+6hu3wrp9rluYuqery0zqdHWRI9mMpSN5FC5j6eWqappEjmQzFkbyKFzGwuGqappEjmQzFkbyKFzGQnFVNU0iR7IZCyN5FC5j"
    "QVxVTZPIkWzGwkgehctYUFdV0yRyJJuxMJJH4TIW4KpqmkSOZDMWRvIoXMZCdVU1TSJHshkLI3kULmOhuaqaJpEj2YyFW+7gMha6"
    "q6ppEinyYF+Hwkg6B5ewcLqqmiaRI9mMhJGRDi4j4XJVNU0iR7IZqY6MdHAZqR6uqqZJJMj5j228j9+Pr7dv6Vg+4/iPqxZX9eln"
    "ohYsMuzXc7df/3637fYsxtevuv0Mv/3T21/eLuq223ehfP2qX5pFlj3Zfsvo92T7La6ubrDIshfbr4x+L7ZfcXV1g0WS9XfZZ/rV"
    "R7/TXfaJuurq6gaLLFvYfjH6LWy/cHV1g0WWFbbfOvoVtt/q6uoGiyyrbL9t9Ktsv83V1Q0WWRZsv330C7bf7urqBossW9l+z9Fv"
    "Zfs9XV3dYJFlG9vvNfptbL+Xq6sbLLIsm69k5KvC5is5XF3dYJFl2XwlI18VNl9JcXV1g0WWZfOVjHxV2Hwl4urqBoskK2y+kpGv"
    "hM1Xoq6ubrDIsmy+kpGvhM1XAldXN1hkWTZfychXwuYrqa6ubrDIsmy+kpGvhM1X0lxd3WCRZdl8JSNfCZuvpLu6usEiy7L5Ska+"
    "EjZfyenq6gaLLMvmKxn5Sth8JZerqxsssiybr3TkK2HzlR6urm6wyLJsvtKRr4TNV1pcXd1gkWXZfKUjXwmbr1RcXd1gkWSVzVc6"
    "8pWy+UrV1dUNFlmWzVc68pWy+Urh6uoGiyzL5isd+UrZfKXV1dUNFlmWzVc68pWy+Uqbq6sbLLIsm6905Ctl85V2V1c3WGRZNl/p"
    "yFfK5is9XV3dYJFl2XylI18pm6/0cnV1g0WWZfMVRr5SNl/hcHV1g0WWZfMVRr5SNl+huLq6wSLLsvkKI18pm68grq5usEiyYPMV"
    "Rr4Cm6+grq5usEix8zcEfC1xv5avXn1DwGf4d+vC1dUNFkl2Ws9P9Fsf/c7rOa5bXV3dYJFlT7bfNvo92X6bq6sbLLKs+0aur6Pn"
    "6NvtObqvX31rwBOV+1y5MJVPV5mb1+kqI8s2fl7XOOrGzuuaKzPzsmfGvn713QVxZXuC6+tX317AsZWelz3/9fWrbzB4onKZK1Pz"
    "EleZm5e4ysiy4Oel46jBzkvnytS84Cpz84KrjCyr/Lxudyhl51XnytS8mqvMzau5ysiyws/rdr0Xdl59rkzN63SVuXmdrjKybOHn"
    "dbveF3Ze11yZmVc7XGVqXu1wlZFlD3pe7Xa9ZxNyK3Nlal7iKnPzElcZSVYufl7jei9swm46V6bmBVeZmxdcZWRZNp+3cbUXNp+3"
    "6urqBoss29l+xz1G2HTemqurGyyybGP7HXcnYdN1666ubrDIspXtd9wThU3H7XR1dYNFlgXb77ibCptu2+Xq6gaLLKtkv33cw4VN"
    "p/1wdXWDRZYVtt9x9xc2Xfbi6uoGiyxb2H5H5hA2HXZxdXWDRZZlX//st7TCpruurq5usEiyhX39s4+MVNh01uHq6gaLLMvmqz7y"
    "VWHzVa+urm6wyLJsvuojXxU2X/Xm6uoGiyzL5qs+8lVh81Xvrq5usMiybL7qI18VNl/109XVDRZZls1XfeSrwuarfrm6usEiy7L5"
    "6hz5qrD56jxcXd1gkWXZfHWOfFXYfHUWV1c3WGRZNl+dI18VNl+d4urqBossy+arc+SrwuarU11d3WCRZA82X50jXx1svjrh6uoG"
    "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"
    "qmkSabLmSDZny8jZB5ezBa6qpkmkyZoj2ZwtI2cfXM6W6qpqmkSarDz5h5eL0WNGfziCV4Wu2ys7L/RtSp/xtz9++PndD3/+8P0P"
    "r6tft1dZXnjd5LHJj9fEnuu+uO4/NcDQMtNgaB208OcNjubPG8bcJHPePI9Nnj1vdXSv/Oyao/nZtXHsmpmd57HJs7Pro3vwszsd"
    "zc/uHMeOzOw8j02end01uq/07ORwND07uV3ramJ2E49Nnpyd3K61jZ+dOJqfnYxjb5nZeR6bPDu7272i87ODo/nZ3a7VPTM7z2OT"
    "Z2d3u1ec/Oyao/nZ3a7VZ2Z2nscmz87udq+4+NmdjuZnd7tWX5nZeR6bPDu7ca/wrxM8RevhaHp2Oq7V0+sFKR6bPDk7HfeKwjuN"
    "iqP52Y1rdck4zcRjh+fNQMe9omTMwL7E74XXTR6bPLtyxr2u8Gag1dH87IaLl4wZTDw2eXZ2415XeDPQ7mh+dn0ce8YMJh6bPDu7"
    "ca8rvBno5Wh+dtc49owZTDw2eXJ2uN3reDNAcTQ9O/tytBdeN3ls8uzsbvc63gygjuZnd7tWZ8xg4rHJs7O73St4M0B1ND+727U6"
    "YwYTj02end3tXsGbAbqj+dndrtUZM5h4bPLs7Ma9QngzwOVofnbjWi0ZM5h4bPLk7Oq4VwhvBrU4mp5dHddqyZjBxGOTZ2c37hXC"
    "e0VVR/OzG9dqyXjFxGOTZ2c37hXCe0WtjuZnN67VkvGKiccmz85u3CuE94raHc3PblyrJeMVE49Nnp3d7V7Be0W9HM3P7natznjF"
    "xGOTJ2fXbvcK3itacTQ9u3a7Vme8YuKxw/Nm0G73iowZNHHVdZPHJs+unNu9jjeDBkfzsxvvd0jGDCYemzw7u9u9jjeD1hzNz268"
    "3yEZM5h4bPLs7Ma9TnkzaKej+dmN9zs0YwYTj02end241ylhBr+x/XAsPbk+3u3QjBdMPHZ4YXsf9znN5PpeXG3d5LHDK9v7uMtp"
    "Jpd3cbV1k8cOD7b3cY/TTK7u6mrrJo8dvrK9jzu0ZnJxh6utmzx2+Mb2Pu7Pmsm1vbrausljh+9s7yNdaCbV9uZq6yaPHf5ke79l"
    "i0wq7d3V1k0eO/zF9n5LRplU2U9XWzd5bPA42N5HLkImFfbL1dZNHjs8m+vOkeuQyXXn4WrrJo8dns1158h1yOS6s7jausljh+df"
    "cT1HskMm2Z3iqusmj02edKFzZDvwr7iecDQ/u5FOkEmGE49Nnp3dyEfgX3E9m6P52Y07PDLJcuKxybOzGxkD/Cuu5+lofna3u2Qm"
    "mU48Nnl2drf7NP967XU4mp7ddbvTZJLtxGOTJ2d33e51/Ou1lzian93tWp1JxhOPTZ6d3e1ewb9ee8HR/Oxu1+pMsp54bPLs7Ma9"
    "ovKv117N0fzsxrW6ZpL5xGOTZ2c37hWVf5LjOh3Nz25cq2sm2U88Nnl2duNeUeknOcpxOJqd3Qt/GK+bPDZ5bnbleLlXfBBz6ZfP"
    "/H6QZ2hx9G12n/HvPvy3f3DQLxfpUVaYsjofdGFoPOiXl06olqujiZbro2X/FY/PlW3zQVMt90GffO3T0UTL52j55A/6GmU7fdD2"
    "Uf4X+vmDts/Qj7LMQZexn15ei2YWVxFHEwc99pP/Yt/nyup80FTLt/1U+drV0UTLt/1U+YO+7SfwB90dTRx0HwcN/qBPV1bnstGt"
    "opyuOnZ45Yd2289KDe2ayzJDk8OVpYcmh6uOHV7oocnteiLM0OyLmUZZamjiyvJDE1cdO3zhh3a7nhVqaDqXpYYGV5YfGlx17PDH"
    "/RtYnjv624WYynJS57LMLU+aK8ue6T7T1Ak7Hc2fsNN1jg1e+Ego43oqVBKVay7LDE0PV5Yemh6uOnb4k17lOq6nQmVZLXNZZpWr"
    "uLLkmVadaeqEwdH8CYPrHDt850/YuCwJleO1zmWpE9ZcWfaE9ZmmTtjpaP6Ena5z7PC8w+jtskQ5jF5zWWbkOFxZcuQojqZHjuKO"
    "HTs871C4XV0oh4LMZamhqSvLD01ddezwoNcpbpdFyuGAuSx1rqory468OZofeXPHjh1e6Ws5bpdFygDR57LUmT5dWfaEXTPNnLB6"
    "OJo+YfVwnWOHF/qE1dtlkbLPWuayzAmr4sqSJ6zqTFMnDI7mTxhc59jhCz+529WFMt9a57LUyJsry468O5ofeXfHjh2eV9B6u7pQ"
    "3lzPuSw1tMuV5Yd2uerY4Av/Lkobl8VCuWs75rLMuWrFlSVH3sTR9MibuGPHDs9LZBuXxUKZb9O5LDU0uLL80OCqY4fnNbCN62mh"
    "7LPVuSx1rpory468O5ofeXfHjh2eF7k2rqeF8sd2zmWpoV2uLD+0y1XHDs8bYL9dTykD7MdclhlaL64sPbReXHXs8LyK9dv1nDLA"
    "LnNZamjqyvJDU1cdOzyvQ/12PaccrmMuSw2turL80Kqrjh2eV5J+u55THtXbXJYaWndl+aF1Vx07PC8V/XYjoFymn3NZamiXK8sP"
    "7XLVscPzNnLebgSUjZzHXJYZ2llcWXpoZ3HVscEfvI2c40ZwUDZyylyWOlfqyrIjh6P5kcMdO3Z43kbOcT0/KBs561yWGlpzZfmh"
    "NVcdOzxvI+e4nh+UjZx9Lkudq9OVZUd+OZof+eWOHWl+/qtJH8oTR2/PFn949XeTPuP/cOSXL6szGB32xGOTv53yJ9oWeyh60MrQ"
    "ZabB0DLokz1h8ng2+NWf9Hpqbi/8YTw2eXbumHun5l5nmpp7G/TFz707+umN8gIe2yB/pj2PTZ490+djXlPeeoq+HE0MbVwW5qD2"
    "XNOexyZPDq2My9KU7J+ii6OfH5r9MfkPr/4k53NNTzw2eXZoMg+NuaYUnWnmmlLG9Wzy16fo6mjihNUxMMmcMM9jk2dPWJuHRp2w"
    "cSGeXmd5ij4dTYz8HC1rZuSexybPjvx2PQU9NDkc/fzQHs+Gv/q72s81PfHY5MmhSZmHxqxTkZlmLixyuyxV/oTB0cQJwxhYzZww"
    "z2OTZ09YnYdGnbDbZanxI++OJkY+Ytb8xsxzI/M8Nnl25Oc8NGrkt8sS7a6ih6OfH7neLgsJd515bPLkyLXMQ2NGrjLTzGVJb5cl"
    "3l0VjiZO2O2ykJHeiccmz56w22WJF09tjiaG1sZBZ/xx4rHJs0Pr89CoVT4uS8Lbp16OJkY+7E8y9jnx2OTJkWNcT4W3TxRHPz80"
    "DPuTjH1OPDZ5dmjjeiq8AUIdTQxtvAwnGQOceGzy7NAwD43Z3KgzzdzCMK6nwvsjuqOJEzZilmT8ceKxybMn7JyHRp2wa6aZE1Zv"
    "lyXeXWtx9PMnrN4uCxl3nXhs8uQJqzIPjTlh9XZZ4u2zwtHEyEfMkox9Tjw2eXbkt8sS74+1OZoY2ohZkvHHiccmzw7tdj3lDbCe"
    "jiaGNl5Nk4wBTjw2eXZot+spb2HtcPTzQ2tDWyVjYROPTZ4cWrvdCHgLa+JoYmgyDjpjYROPTZ4dms5DY24jbdwIlLewVh1NjHy8"
    "O6EZC5t4bPLsyNs8NGrk43qqvMO109HEyMf1TDMON/HY5NmRX/PQmJH3Y6aZdNvHZUl5f+zi6OdPWB+XBc3448RjkydPWNd5aNQJ"
    "u12WeAPs1dHEyG+XhYwBTjw2eXbkbR4aNfI+09QeuV2WeAPsl6OJEzZeTdOMAU48NnnyhJ23yxLvcGdx9PNDO4e2Ku9Qp8wHzayy"
    "U2eaWWXn7bLA+9tZHU0M7LYteX8623zQ1MBu25K3r/N0NNHy7W7N2895zQfNtHzdtgXvTldx9PMtX7dtwbvLddsWvLtc6mjioMeL"
    "vMq7wzX2E/j0f1VHEwc99hP49G3f0v3t/MW938ozdHf07aC/feZZ7hf+uFUvTPXTVVe6+umqY4P3y+W5o78eRz+t0ierX3N1YnZq"
    "D4S/0Ozs1L6l+oXHDn+ys1N7IP3bV99g/GT1MlenZieuOj87cdWxw3d+djqOvmdmp3N1anZw1fnZwVXHDt/42dVx9C0zuzpXp2bX"
    "XHV+ds1Vxw5f+dmNe8WUsJ+s3ufq1OxOV52f3emqY4fH80f/mb3dKZCZ3OVq6yaPHV7J3svtPqWJ3svhausmjx1e2N5vdznJ9F5c"
    "bd3kscMXtvfbPbZkehdXWzd57PAH2/vtDn1keldXWzd5bPBysb2PfCBXpne42rrJY4c/2d5HupBMqi3V1dZNHjt8Z3sf2UYyqbQ0"
    "V1s3eezwje19JCPJpMrSXW3d5LHD87mwjGQmmVxYzrk6kwvL5arzs7tcdezwoGcnIx1JJhnKMVdnZifFVadnJ8VVxw6v/NGPhCGZ"
    "ZCniqusmjx1e+O5HxpBMthR11XWTxw5f+O5vKSOTLgWuum7y2OEPvvtbzsjkS6muum7y2OAL/8qnjKRRMglTmquumzx2eP61SxlZ"
    "o2QypnRXXTd57PD8q48yskbJpEw5XXXd5LHD868fysg6JZMz5XLVdZPHDs8nPR1ZqWRyph6uum7y2OH5rKYjq5VMUtTiqusmjx2e"
    "z3o6sl7JZD0VV103eezwfNbTkfVKJuupuuq6yWOH57OejqxXMllP4arrJo8dns96OrJeyWQ9ra66bvLY4P0XQT7X/ch6RybraZur"
    "M3au3VVnz9zpaH7ypzt27PDuy8eeO/qRNY5MztRrrs6cdxyuOnneUGaaOW8QR9PnDeI6xw7PPx2Aca84MgkZOlenZgdXnZ8dXHVk"
    "+T++XGgfR//H6G+vf/qz7Ye9WfmC3w7/j+Gffr/9A4/62P0Hxsl/soHiGhi77klcZhwUrjNeKRwz3ii8znin8DbjJ4X3Gb8o/Jzw"
    "jyeSwa8Zp9a9HDNOrTrxq07IXSe3VSf8phV1uMx7bvng5Y08jOS3+/QPYPcfYLe7YJ4ctd2lzji13aXNOLXdpc84td3lnHFqu8s1"
    "49R212PGqe2uZcK57a7zjuG2u+qMU6tOb6tO+f2q1eHEfjWXeCH5/Tr9A9j9B9j9qm2eHLVftc84tV/1nHFqv+o149R+xTHj1H5F"
    "mXFqv0JmnNqv0Ann9ivmHcPtV9QZp1YdbqsO/H5FdzixX+0rXF5Ifr9O/wB2/wF2v+KcJ0ftV1wzTu3Xesw4tV9rmXFqv1aZcWq/"
    "Vp1xar9WzDi1X2udcG6/1nnHcPu19hmnVl29rbrK79d6OZzYr/YnJF9Ifr9O/wB2/wF2v7Zjnhy1X1uZcWq/Nplxar82nXFqvzbM"
    "OLVfW51xar+2NuPUfm19wrn92uYdw+3Xds04ter6bdU1fr/24nBiv9pf23sh+f06/QPY/QfY/dplnhy1X7vOOLVfO2ac2q+9zji1"
    "X3ubcWq/9j7j1H7t54xT+7VfE87t13PeMdx+PcuMU6vuvK26zu/XUx1O7Nfz9mpPz+zX6R/A7j/A7tcT8+So/XredsyZGHxzODP4"
    "Nto+U4P3/wB2/wF68H2eHDf4l93+58fE//wU9nGXH58xIbBP31jwgimDlQcGBpMHVhlMH1hjMDywzmD1gZ0M1h7YxWDdMLssP4ed"
    "A6O4a3DE8vr0/x6cMNxjoRSq3GOhFKraY6EUZbDHQilgsMdCKZXBHgulNAYbC6Uz2FgoJ4ONdUKs5lIey0QOBnusEmFWSXmsEmFW"
    "SXmsEmFWSXmsEmFWSXmsEmFWSXmsEmFWSXmsEmFWSXmsEmFWSXmsEmFWiTxWiTKrRB6rRJlVIo9VoswqkccqUWaVyGOVKLNK5LFK"
    "lFkl8lglyqwSeawSZVaJPFaJMqtEHqtEmVWij1UCZpXoY5WAWSX6WCVgVok+VgmYVaKPVQJmlehjlYBZJfpYJWBWiT5WCZhVoo9V"
    "AmaV6GOVgFkleKySyqwSPFZJZVYJHqukMqsEj1VSmVWCxyqpzCrBY5VUZpXgsUoqs0rwWCWVWSV4rJLKrBI8VkllVkl9rJLGrJL6"
    "WCWNWSX1sUoas0rqY5U0ZpXUxyppzCqpj1XSmFVSH6ukMaukPlZJY1ZJfaySxqyS+lgljVkl7bFKOrNK2mOVdGaVtMcq6cwqaY9V"
    "0plV0h6rpDOrpD1WSWdWSXusks6skvZYJZ1ZJe2xSjqzStpjlXRmlfTHKjmZVdIfq+RkVkl/rJKTWSX9sUpOZpX0xyo5mVXSH6vk"
    "ZFZJf6ySk1kl/bFKTmaV9McqOZlV0h+r5GRWyflYJRezSs7HKrmYVXI+VsnFrJLzsUouZpWcj1VyMavkfKySi1kl52OVXMwqOR+r"
    "5GJWyflYJRezSsbLrxezSuzl1/8PbAxbeAXlAwA="
)

# Maestro extraído del ADC PMONTT (maestro_slotting.csv), comprimido.
MAESTRO_SLOTTING = (
    "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"
)


# =====================================================================================
#  INTERFAZ (Streamlit) — solo subir archivos y imprimir
# =====================================================================================
import streamlit as st
from datetime import datetime

st.set_page_config(page_title="Reabasto Faltantes · PMONTT", page_icon="🏗️", layout="wide",
                   initial_sidebar_state="collapsed")
st.markdown("""
<style>
  [data-testid="stSidebar"], [data-testid="collapsedControl"] {display:none;}
  .paso {font-size:20px; font-weight:700; margin: 6px 0;}
  .ok   {color:#16A34A; font-size:18px; font-weight:700;}
  .no   {color:#9CA3AF; font-size:18px;}
</style>""", unsafe_allow_html=True)


def maestros():
    ub = pd.read_csv(io.BytesIO(gzip.decompress(base64.b64decode(MAESTRO_UBICACIONES))), dtype=str)
    sl = pd.read_csv(io.BytesIO(gzip.decompress(base64.b64decode(MAESTRO_SLOTTING))), dtype=str)
    return ub, sl


def identificar(archivo):
    """Reconoce qué archivo es por sus columnas (el operador no tiene que elegir)."""
    try:
        cab = _leer_csv(archivo).columns
    except Exception:
        return None
    if "Cantidad de seleccion" in cab:
        return "lineas"
    if "Vida util" in cab or "Fecha de caducidad" in cab:
        return "vida"
    if "estado_de_inventario" in cab and "cantidad" in cab:
        return "cuadratura"
    return None


NOMBRES = {"lineas": "Líneas de pedido sin inventario",
           "cuadratura": "Cuadratura de stock",
           "vida": "Operaciones de vida útil"}

st.title("🏗️ Reabasto de faltantes — CD Puerto Montt")
st.markdown('<div class="paso">Arrastra aquí los 3 archivos del WMS (pueden ir todos juntos)</div>',
            unsafe_allow_html=True)
subidos = st.file_uploader("Archivos CSV", type="csv", accept_multiple_files=True,
                           label_visibility="collapsed")

archivos, desconocidos = {}, []
for f in subidos or []:
    tipo = identificar(f)
    if tipo:
        archivos[tipo] = f
    else:
        desconocidos.append(f.name)

cols = st.columns(3)
for col, (clave, nombre) in zip(cols, NOMBRES.items()):
    if clave in archivos:
        col.markdown(f'<div class="ok">✅ {nombre}</div>', unsafe_allow_html=True)
    else:
        col.markdown(f'<div class="no">⬜ {nombre}</div>', unsafe_allow_html=True)
for n in desconocidos:
    st.warning(f"No reconozco el archivo **{n}**. Debe ser uno de los 3 exportes del WMS.")

if len(archivos) < 3:
    st.stop()

try:
    ubic, slot = maestros()
    faltantes, lineas = cargar_faltantes(archivos["lineas"])
    cuad = cargar_cuadratura(archivos["cuadratura"])
    vu = cargar_vida_util(archivos["vida"])
    movs, resumen = calcular(faltantes, cuad, vu, ubic, slot)
except Exception as e:
    st.error(f"❌ No se pudieron procesar los archivos. Avisa al planificador. Detalle: {e}")
    st.stop()

st.divider()
if movs.empty:
    st.success("No hay nada que bajar desde almacenamiento con los archivos cargados.")
else:
    fecha_hora = datetime.now().strftime("%d-%m-%Y %H:%M")
    df_final = movs.copy()
    df_final["Vencimiento"] = pd.to_datetime(df_final["Vencimiento"]).dt.strftime("%d-%m-%Y")
    n_pallet = int((df_final["Tipo"] == "Pallet completo").sum())
    st.markdown(f'<div class="paso">Listo: {len(df_final)} movimientos para la grúa '
                f'({n_pallet} pallets completos). Presiona Imprimir.</div>', unsafe_allow_html=True)

    filas_html = ""
    for idx, r in df_final.iterrows():
        tipo = "PA" if r["Tipo"] == "Pallet completo" else "CJ"
        tipo_color = "#7C2D12" if tipo == "PA" else "#1E40AF"
        filas_html += f"""
        <tr>
            <td style="text-align:center;">{idx + 1}</td>
            <td style="font-weight:bold;font-family:monospace;font-size:14px;">{r['SKU']}</td>
            <td>{r['Descripción']}</td>
            <td style="text-align:center;font-weight:bold;color:#1E3A8A;font-size:15px;">{r['Origen']}</td>
            <td style="text-align:center;font-weight:bold;color:#065F46;font-size:15px;">{r['Destino']}</td>
            <td style="text-align:center;font-weight:bold;font-size:15px;color:{tipo_color};">{tipo}</td>
            <td style="text-align:center;font-weight:bold;font-size:16px;">{r['Cajas a mover']}</td>
        </tr>"""

    html_report = f"""
    <style>
        @media print {{
            body {{ font-family: Arial, sans-serif; margin:0; padding:0; }}
            .no-print {{ display:none !important; }}
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
    <div style="margin-bottom:15px;text-align:center;" class="no-print">
        <button onclick="window.print()" style="background:#2563EB;color:white;font-weight:bold;
            padding:16px 40px;font-size:20px;border:none;border-radius:8px;cursor:pointer;">
            🖨️ Imprimir Hoja de Ruta
        </button>
    </div>
    <div class="report-card">
        <table class="header-table">
            <tr>
                <td class="title">Reabasto / Hoja de Ruta — CD Puerto Montt</td>
                <td style="text-align:right;font-size:12px;"><strong>Fecha Emisión:</strong> {fecha_hora}</td>
            </tr>
            <tr><td colspan="2" style="font-size:13px;color:#475569;">
                <strong>Movimientos:</strong> {len(df_final)} &nbsp;|&nbsp;
                <strong>Pallets completos:</strong> {n_pallet} &nbsp;|&nbsp;
                PA = pallet completo &nbsp; CJ = cajas
            </td></tr>
        </table>
        <table class="data-table">
            <thead><tr>
                <th>#</th><th>SKU</th><th>Descripción</th><th>Origen</th>
                <th>Destino</th><th>Tipo</th><th>Cajas</th>
            </tr></thead>
            <tbody>{filas_html}</tbody>
        </table>
    </div>"""
    st.components.v1.html(html_report, height=900, scrolling=True)

# Detalle para el planificador / supervisor (cerrado por defecto)
with st.expander("Detalle para supervisor (SKUs sin stock y alertas)"):
    vista = resumen.copy()
    vista["Venc. picking"] = pd.to_datetime(vista["Venc. picking"]).dt.strftime("%d-%m-%Y")
    pendientes = vista[vista["Estado"] != "OK"]
    if not pendientes.empty:
        st.markdown("**No se pudieron cubrir desde almacenamiento:**")
        st.dataframe(pendientes, use_container_width=True, hide_index=True)
    st.markdown("**Resumen completo por SKU:**")
    st.dataframe(vista, use_container_width=True, hide_index=True)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        if not movs.empty:
            df_final.drop(columns=["secuencia_viaje"]).to_excel(xw, sheet_name="Hoja de ruta", index=False)
        vista.to_excel(xw, sheet_name="Resumen SKU", index=False)
    st.download_button("⬇️ Descargar Excel", buf.getvalue(),
                       file_name=f"reabasto_pmontt_{datetime.now():%Y%m%d_%H%M}.xlsx")
