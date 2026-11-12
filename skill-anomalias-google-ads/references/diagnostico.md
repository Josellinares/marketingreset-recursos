# Diagnóstico de alertas

Cruza las señales: una rotura casi nunca aparece en una sola. Esta tabla dice qué combinación apunta a qué causa y qué revisar.

| Patrón | Causa probable | Revisión |
|---|---|---|
| Sesiones GA4/clic ↓ **y** orgánico+directo ↓ | **Web caída o muy lenta** | Abrir la web en móvil y escritorio; hosting/CDN; despliegues del día |
| Sesiones GA4/clic ↓, orgánico normal | Landings de Ads rotas, redirección o GA4 no carga en ellas | Visitar las URLs de Ads; parámetros de URL; consentimiento |
| Visita: NO RESPONDE / 4xx / 5xx | Landing caída o eliminada | Confirmar a mano; si persiste, pausar los anuncios que apuntan ahí (proponer, no hacer) |
| Visita: PÁGINA DE ERROR o FORMULARIO DESAPARECIDO | Cambio en la web | Preguntar al cliente o al equipo web por cambios recientes |
| Visita: LENTA (> 8 s) | Rendimiento de la web | Repetir; PageSpeed Insights |
| Anuncios rechazados por destino (en campañas con tráfico) | URL rota o web caída para Google | Revisar las URLs listadas; tras arreglarlas, solicitar revisión |
| Acción Ads / clics ↓ con GA4 normal | Etiqueta de Ads rota | GTM (etiqueta de conversión y su activador), Tag Assistant |
| Evento GA4 / sesiones ↓ con Ads normal | GA4 o GTM roto | Contenedor GTM publicado, configuración de GA4 |
| Ads ↓ **y** GA4 ↓, sesiones normales | **Formulario no envía** o página de gracias rota | Enviar un formulario real de prueba |
| Formularios ↓, llamadas normales | Problema en la web (no en la demanda) | Ídem formulario y web |
| Embudo: paso N / paso N−1 ↓ | Rotura en carrito, checkout, pago o su etiqueta | Compra de prueba hasta el paso que falla |
| Una etiqueta ×2 frente a la otra | Disparo duplicado (activador duplicado, recarga de la página de gracias) | GTM: activadores; recuento de la acción (una o cada una) |
| Ads y GA4 suben ×2 a la vez | Pico real de demanda… o spam | Calidad de los leads en el CRM |
| Clics/impresiones ↓ ≤ 10 % de lo esperado | **Cuenta casi parada** | Facturación, rechazos masivos, campañas pausadas |
| Clics/impresiones ↓ con cambios de presupuesto o estado los días previos | Decisión del equipo (🟡) | Confirmar que era intencionado |
| Clics/impresiones ↓ sin cambios en la cuenta | Límite de presupuesto, pujas, políticas, competencia | Estado de campañas, cuota de impresiones, pujas |
| Solo una URL con 0 conversiones (7 días) | Formulario de esa landing | Probar el formulario de esa URL |

## Orden de revisión

1. Si hay 🔴 de visita, de web o de política: **la web primero**. Todo lo demás puede ser consecuencia.
2. Después, coherencia entre etiquetas (¿falla una o fallan las dos?).
3. Por último, tráfico, cruzado con el historial de cambios.

## Qué no es una rotura

- Caídas por estacionalidad o por cambios de presupuesto: el modelo compara por día de la semana y cruza el historial de cambios.
- El último día con datos (⚪): siempre llega incompleto.
- Llamadas a 0 en fin de semana si la extensión de llamada solo está activa entre semana: el modelo lo aprende de la referencia.
