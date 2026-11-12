---
name: vigilancia-roturas-google-ads
description: Vigilancia diaria de roturas técnicas en cuentas de Google Ads (web caída o lenta, formularios que no envían, etiquetas que no saltan o duplican, tráfico parado, landings rotas) con base estadística, usando Ads, GA4 y visita a las URLs. Úsala para la revisión diaria o cuando se sospeche que algo se ha roto.
---

# Vigilancia de roturas — Google Ads + GA4 + landings

Skill de SOLO LECTURA. Responde a una pregunta: **¿se ha roto algo técnico en la cuenta?** No mide rendimiento ni optimiza; para eso están `auditoria-google-ads` y `detector-variaciones-conversion`.

Detecta:
- Web caída o lenta.
- Formulario que no envía.
- Etiqueta que deja de saltar o que dispara de más.
- Rotura en un paso del embudo (carrito, checkout, pago).
- Tráfico parado (facturación, rechazos, pausas).
- Landing con error, sin formulario o sin conversiones.
- Anuncios rechazados por "destino no funciona".

El destinatario es un experto que decide si revisa o no: **salida ejecutiva, sin explicar el método** salvo que lo pida.

## Configuración inicial (obligatoria)

**La skill no trae cuentas configuradas: sin al menos una cuenta, no se ejecuta.** El motor lo comprueba antes de consultar nada y, si falta algo, devuelve exactamente qué.

**Requisitos:** Composio con las apps `googleads` (obligatoria) y `google_analytics` (opcional) conectadas.

Datos por cuenta (formato en `references/cuentas.plantilla.json`):

| Campo | Qué es | Dónde se encuentra |
|---|---|---|
| `id` | Alias corto, sin espacios (p. ej. `cliente-leads`) | Lo eliges tú |
| `nombre` | Nombre que aparece en el informe | Lo eliges tú |
| `customer_id` | ID de cliente de Google Ads, 10 dígitos (con o sin guiones) | Google Ads, arriba a la derecha |
| `acceso.conexion` | Conexión de Composio que entra en la cuenta | `COMPOSIO_MANAGE_CONNECTIONS` (`list`) o la respuesta de `COMPOSIO_SEARCH_TOOLS`; valor tipo `googleads_xxxx-yyyy` |
| `acceso.login_customer_id` | ID de la MCC, solo si ninguna conexión entra directamente | Google Ads de la MCC, arriba a la derecha |
| `ga4` | Propiedad GA4 (`properties/<número>`) o `null` | GA4 → Administrar → Detalles de la propiedad |
| `estado` | `activa` o `pendiente` | — |

GA4 es opcional, pero sin ella no se detectan web caída o lenta ni discrepancias entre etiquetas de Ads y GA4.

**Dónde guardar la configuración:** fuera de la skill, nunca dentro de ella. Lo recomendable es incluir la lista de cuentas en las instrucciones de la tarea programada y pasarla al motor con `informe_diario(cuentas=[...])`, que además la guarda en Composio. Así no se pierde aunque se vacíe el almacenamiento.

**Si no hay configuración:** en una sesión con el usuario, pide los datos y haz el alta guiada (ver "Alta de una cuenta"). En una tarea programada, no ejecutes nada: informa "⚠️ Vigilancia no ejecutada: falta configurar las cuentas".

## Arquitectura

Todo el cálculo lo hace un script determinista, no el modelo. Vive en el workbench de Composio (persistente):

| Fichero | Qué es |
|---|---|
| `/mnt/files/vigilancia/motor.py` | El motor. Copia de `scripts/motor.py`. |
| `/mnt/files/vigilancia/cuentas.json` | Cuentas vigiladas: ID, acceso, propiedad GA4, estado. Formato en `references/cuentas.plantilla.json` (ver "Configuración inicial"). |
| `/mnt/files/vigilancia/historial/` | Resultado de cada día y estado de las landings (para detectar cambios: formulario que desaparece, título de error). |

## Flujo

1. **Comprobar el motor.** En `COMPOSIO_REMOTE_WORKBENCH`, comprueba que existen `motor.py` y `cuentas.json`. Si falta el motor, escríbelo con el contenido de `scripts/motor.py`. Si falta `cuentas.json` y la lista de cuentas no viene en las instrucciones, aplica "Si no hay configuración" (ver "Configuración inicial").
2. **Ejecutar.** Termina la celda con una expresión: el workbench no siempre muestra `print`.
   ```python
   exec(open('/mnt/files/vigilancia/motor.py').read())
   texto, res = informe_diario()            # o informe_diario(['cliente-leads']) o informe_diario(cuentas=[...])
   texto
   ```
   Tarda unos 40 s con 6 cuentas. Si la celda pasa de 180 s, ejecuta por grupos de cuentas.
3. **Verificar antes de avisar** (obligatorio para 🔴 y 🟠):
   - Re-deriva la cifra de la alerta con una query GAQL o GA4 independiente del día concreto.
   - **Tráfico:** mira el historial de cambios (`change_event`) de esos días. Si hay cambios de presupuesto, estado o puja, es probablemente intencionado → 🟡.
   - **Landing:** repite la visita a la URL.
   - **⚪ provisional** (solo el último día): no se avisa como rotura. Se re-verifica en la siguiente ejecución.
4. **Entregar** la tabla ejecutiva (ver "Salida"). Si todo está en 🟢, basta con una línea.

Nunca modifiques la cuenta. Si una acción es urgente (pausar campañas que llevan a un 404), proponla y espera confirmación.

## Alta de una cuenta

Pide al usuario solo lo que no puedas averiguar:

1. **ID de cliente de Google Ads** (y nombre).
2. **Acceso:**
   - Prueba `SELECT customer.id FROM customer` con cada conexión `googleads` de Composio (`COMPOSIO_MANAGE_CONNECTIONS` con `list`).
   - Si todas dan `USER_PERMISSION_DENIED`, pide el **ID de la MCC** y usa `acceso.login_customer_id` (el motor llamará a la API con esa cabecera).
3. **GA4:** pide el ID de la propiedad. Si no lo tienen, búscala con `GOOGLE_ANALYTICS_LIST_ACCOUNT_SUMMARIES` por nombre o dominio. **Valídala siempre:** la propiedad correcta recibe sesiones con `sessionGoogleAdsCustomerId` = el ID de la cuenta, en una cantidad ≥ 20 % de los clics de las últimas 4 semanas (el modo de consentimiento la reduce; 0,3–1,3 es normal). Si no hay sesiones de ese ID, es otra propiedad. Sin GA4 válida, el motor funciona solo con Ads.
4. Añade la entrada a la configuración (instrucciones de la tarea y `cuentas.json`). Las acciones de conversión, el embudo, las URLs y los pares de control **se autoconfiguran** cada día; no hace falta listarlos.

Una cuenta aún sin datos se da de alta con `"estado": "pendiente"` y aparece como ⚪ en el informe.

## Método (resumen para explicarlo si lo piden)

**Señales diarias** (se construyen solas a partir de lo que tenga la cuenta):

| Señal | Qué detecta |
|---|---|
| Clics, impresiones | Tráfico parado |
| Cada acción de conversión / clics (por fecha de conversión, sin lag) | Formulario o etiqueta rota |
| Etiqueta Ads frente a etiqueta GA (misma categoría) | Una etiqueta deja de saltar o duplica |
| Formularios frente a llamadas desde anuncio | Problema en la web (las llamadas no dependen de ella) |
| Embudo: carrito/vista, pago/carrito, compra/pago | Rotura en un paso del checkout |
| Sesiones GA4 de Ads / clics | Web caída o lenta (llega el clic, no la sesión) |
| Orgánico + directo GA4 | Control: si cae también, es la web y no Ads |
| Evento clave GA4 / sesiones | GTM o formulario |
| Conversiones por URL (7 días) | Landing concreta rota |
| Visita a las landings principales | 4xx/5xx, página de error, > 8 s, formulario desaparecido |
| Anuncios rechazados por destino (en campañas con tráfico) | URL rota o web caída |

**Estadística:**
- Esperado `λ = denominador del día × tasa del mismo día de la semana` en una referencia **fija** de 6 semanas anterior a la ventana vigilada (14 días). Una referencia móvil se contamina con la propia rotura y deja de detectarla.
- Variabilidad real de cada señal: sobredispersión φ (binomial negativa) y dispersión robusta de `log(obs/λ)`.
- Reglas:
  - **CAÍDA:** `obs/λ ≤ 0,5`, `z_log ≤ −3`, `P_NB < 0,001`, `λ ≥ 5`.
  - **SUBIDA:** ×2 y +5, `z_log ≥ 3`, `P_NB < 0,001`.
  - **Acumulado de 3 días:** para series de 2–5 al día.
  - **Racha de ceros:** `P_NB(0 | Σλ) < 0,001`.
  - **CUSUM inferior** con caída media ≥ 30 % (degradación sostenida, como una web lenta).
- Solo se informan las alertas de los **últimos 3 días**. El último es provisional.

**Validación con datos reales (oct 2026).** En dos cuentas reales de captación de leads (educación e inmobiliaria) se inyectaron roturas sobre sus datos:

| Rotura simulada | Detección |
|---|---|
| Web caída | Mismo día |
| Formulario roto | Mismo día |
| Etiqueta rota | Mismo día |
| Web lenta (−35 %) | +1 día |
| Duplicado | +1–2 días |

Falsas alarmas: unas 4 en 64 días, casi todas eventos reales.

## Salida

```
**Vigilancia de roturas — datos hasta <fecha>**

| Cuenta | Estado | Resumen |
|---|---|---|
| Cuenta A · Leads | 🟢 | 18 señales OK · con GA4 |
| Cuenta B · Tienda | 🟡 | 2 aviso(s) · con GA4 |

**<Cuenta con alerta>**
| Señal | Estado | Cuándo | Obs. vs esperado | Lectura |
```

Estados:
- 🔴 Rotura probable: actuar hoy.
- 🟠 Revisar.
- 🟡 Aviso, probablemente intencionado o atípico.
- ⚪ Provisional o pendiente.
- 🟢 OK.

Debajo, como mucho 3 líneas con la revisión propuesta. Detalle del diagnóstico en `references/diagnostico.md`.

## Limitaciones conocidas

- La visita a las URLs se hace desde fuera de la red del cliente. Si la web bloquea bots, puede dar falsos "no responde". Confirma siempre a mano antes de un 🔴 de visita.
- Las conversiones importadas del CRM (`UPLOAD_*`, Salesforce) no se vigilan a diario: llegan con días de retraso.
- Con menos de unas 2 conversiones/día, la acción solo se vigila por rachas de ceros y acumulado de 3 días: la detección tarda 2–4 días.
- El historial de cambios de Google Ads solo cubre 30 días.

## Versiones

- **1.1** — La skill ya no incluye cuentas reales: plantilla `references/cuentas.plantilla.json`, sección "Configuración inicial", validación de la configuración en el motor y `informe_diario(cuentas=[...])`.
- **1.0** — Primera versión.
