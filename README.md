# Reabasto de faltantes con FEFO — CD CCU Puerto Montt

App Streamlit que genera la hoja de ruta de la grúa para cubrir las líneas de pedido sin inventario,
eligiendo siempre el LPN que vence primero.

## Uso diario
Subir en la barra lateral los 3 exportes del WMS (CSV):
1. OPERACIONES_LINEAS_DE_PEDIDO_SIN_INVENTARIO
2. CUADRATURA_DE_STOCK
3. OPERACIONES_DE_VIDA_UTIL

## Reglas
- Faltante por SKU = suma de "Cantidad de seleccion" de las líneas.
- Origen: solo área ALMAC con estado D (opcional STAGE). El LPN debe existir en la cuadratura.
- FEFO: fecha de caducidad más próxima; con la misma fecha, restos primero (LPN bajo la norma de pallet).
- Destino: ubicación de picking del ADC (pestaña 25); si el SKU no está, la ubicación PICK de la cuadratura.
- Tipo: zona de movimiento del destino (pestaña 16): ZM-PT01 = pallet completo, ZM-PT01CJ = cajas.
- Hoja de ruta ordenada por secuencia de viaje del origen.

## Maestro ADC
`data/maestro_ubicaciones.csv` y `data/maestro_slotting.csv` se extrajeron del ADC PMONTT.
Si el ADC cambia, se puede subir el .xlsx desde "Maestro ADC" en la barra lateral.
