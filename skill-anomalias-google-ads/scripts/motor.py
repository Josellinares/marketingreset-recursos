"""
Vigilancia de roturas — Google Ads (+ GA4 opcional) + comprobación activa de landings.

Se ejecuta en el workbench de Composio (usa sus helpers globales run_composio_tool y
proxy_execute). Uso:
    exec(open('/mnt/files/vigilancia/motor.py').read())
    texto, res = informe_diario()                    # todas las cuentas activas de cuentas.json
    texto, res = informe_diario(['cuenta-leads'])    # solo algunas
    texto, res = informe_diario(cuentas=[...])       # cuentas pasadas directamente (se guardan en cuentas.json)

Requiere configurar al menos una cuenta (ver "Configuración inicial" en SKILL.md y la plantilla
references/cuentas.plantilla.json). Sin configuración válida no consulta nada y devuelve qué falta.

Detecta roturas técnicas graves (web caída o lenta, formularios que no envían, etiquetas que
no saltan o duplican, tráfico parado, landings rotas). No mide rendimiento.

Método (por señal diaria):
  - Esperado λ = denominador del día × tasa del mismo día de la semana en una referencia
    FIJA de 6 semanas anterior a la ventana vigilada (14 días). Si no hay denominador,
    mediana del mismo día de la semana.
  - Sobredispersión φ (binomial negativa) y dispersión de log(obs/λ) estimadas en la referencia.
  - Reglas: CAÍDA (obs/λ ≤ 0,5, z_log ≤ −3, P_NB < 0,001, λ ≥ 5) · SUBIDA (×2, +5, z_log ≥ 3,
    P_NB < 0,001) · acumulado 3 días (λ ≥ 8) · racha de ceros (P_NB(0) < 0,001) ·
    CUSUM inferior sobre z_log con caída media ≥ 30 %.
  - El último día con datos es PROVISIONAL (conversiones aún sin procesar).
"""
import json, math, re, time, io, contextlib, datetime as dt
import numpy as np
import pandas as pd
import requests
from scipy import stats

BASE = '/mnt/files/vigilancia/'
CONFIG_PATH = BASE + 'cuentas.json'
HIST_DIR = BASE + 'historial/'
REF_DAYS, WIN_DAYS, RECENT_DAYS = 42, 14, 3


def num(x):
    try:
        return float(x)
    except Exception:
        return 0.0


# ---------------------------------------------------------------- acceso a datos
def _rows(r):
    d = r.get('data', r) if isinstance(r, dict) else r
    if isinstance(d, dict):
        return d.get('results', []) or []
    if isinstance(d, list):
        return sum([(b or {}).get('results', []) or [] for b in d], [])
    return []


def gaql(acc, q):
    """GAQL contra la cuenta. Con login_customer_id usa la API directa (MCC distinta)."""
    a = acc['acceso']
    if a.get('login_customer_id'):
        rows, tok = [], None
        while True:
            body = {'query': q}
            if tok:
                body['pageToken'] = tok
            if True:
                r, e = proxy_execute('POST', f"/v26/customers/{acc['customer_id']}/googleAds:search",
                                     'googleads', body=body,
                                     headers={'login-customer-id': a['login_customer_id']})
            if e:
                raise RuntimeError(str(e)[:300])
            rows += r.get('results', []) or []
            tok = r.get('nextPageToken')
            if not tok:
                return rows
    if True:
        r, e = run_composio_tool('GOOGLEADS_SEARCH_STREAM_GAQL',
                                 {'query': q, 'customer_id': acc['customer_id']}, account=a['conexion'])
    if e:
        raise RuntimeError(str(e)[:300])
    return _rows(r)


def ga4(prop, reqs):
    if True:
        r, e = run_composio_tool('GOOGLE_ANALYTICS_BATCH_RUN_REPORTS', {'property': prop, 'requests': reqs})
    if e:
        raise RuntimeError(str(e)[:300])
    out = []
    for rep in ((r or {}).get('data') or r).get('reports') or []:
        dh = [h['name'] for h in rep.get('dimensionHeaders') or []]
        mh = [h['name'] for h in rep.get('metricHeaders') or []]
        out.append(pd.DataFrame([
            dict(zip(dh, [v.get('value') for v in x.get('dimensionValues', [])]))
            | {m: num(v.get('value')) for m, v in zip(mh, x.get('metricValues', []))}
            for x in rep.get('rows') or []]))
    return out


def ga4_key_events(prop):
    if True:
        r, e = run_composio_tool('GOOGLE_ANALYTICS_LIST_KEY_EVENTS', {'parent': prop, 'pageSize': 200})
    if e:
        return []
    return [k.get('eventName') for k in ((r or {}).get('data') or {}).get('keyEvents') or []]


# ---------------------------------------------------------------- descarga
WEB_EXCLUIR_TIPOS = ('YOUTUBE', 'STORE', 'UNKNOWN', 'FIREBASE', 'SALESFORCE', 'UPLOAD')
FUNNEL = ['PAGE_VIEW', 'DEFAULT', 'ADD_TO_CART', 'BEGIN_CHECKOUT', 'PURCHASE']


def descargar(acc, d0, d1):
    q = (f"SELECT segments.date, metrics.clicks, metrics.impressions, metrics.cost_micros "
         f"FROM customer WHERE segments.date BETWEEN '{d0}' AND '{d1}'")
    t = pd.DataFrame([{'date': r['segments']['date'], 'clicks': num(r['metrics'].get('clicks')),
                       'impr': num(r['metrics'].get('impressions')),
                       'cost': num(r['metrics'].get('costMicros')) / 1e6} for r in gaql(acc, q)])
    S = t.assign(date=pd.to_datetime(t.date)).set_index('date').sort_index()
    S = S.reindex(pd.date_range(d0, d1)).fillna(0)

    # conversiones por FECHA DE CONVERSIÓN (sin lag), todas las acciones
    q = (f"SELECT segments.date, segments.conversion_action_name, metrics.all_conversions_by_conversion_date "
         f"FROM customer WHERE segments.date BETWEEN '{d0}' AND '{d1}'")
    c = pd.DataFrame([{'date': r['segments']['date'], 'action': r['segments'].get('conversionActionName'),
                       'conv': num(r['metrics'].get('allConversionsByConversionDate'))} for r in gaql(acc, q)])
    q = ("SELECT conversion_action.name, conversion_action.type, conversion_action.category, "
         "conversion_action.status FROM conversion_action WHERE conversion_action.status != 'REMOVED'")
    ca = {r['conversionAction']['name']: r['conversionAction'] for r in gaql(acc, q)}
    acciones = {}
    if not c.empty:
        c['date'] = pd.to_datetime(c.date)
        pv = c.pivot_table(index='date', columns='action', values='conv', aggfunc='sum')
        for a in pv.columns:
            info = ca.get(a, {})
            tipo, cat = info.get('type', ''), info.get('category', '')
            if any(tipo.startswith(x) for x in WEB_EXCLUIR_TIPOS) or cat in ('ENGAGEMENT',):
                continue
            col = 'conv::' + a
            S[col] = pv[a].reindex(S.index).fillna(0)
            acciones[col] = {'nombre': a, 'tipo': tipo, 'categoria': cat,
                             'clase': 'llamada' if tipo == 'AD_CALL' else 'web',
                             'familia': 'ga' if ('ANALYTICS' in tipo) else 'ads'}
    return S, acciones


def descargar_ga4(acc, d0, d1):
    prop, cid = acc['ga4'], acc['customer_id']
    f_ads = {'filter': {'fieldName': 'sessionGoogleAdsCustomerId', 'stringFilter': {'value': cid}}}
    kev = [k for k in ga4_key_events(prop) if k][:20]
    reqs = [
        {'dateRanges': [{'startDate': d0, 'endDate': d1}], 'dimensions': [{'name': 'date'}],
         'metrics': [{'name': 'sessions'}], 'dimensionFilter': f_ads, 'limit': 1000},
        {'dateRanges': [{'startDate': d0, 'endDate': d1}],
         'dimensions': [{'name': 'date'}, {'name': 'sessionDefaultChannelGroup'}],
         'metrics': [{'name': 'sessions'}], 'limit': 5000},
    ]
    if kev:
        f_ev = {'filter': {'fieldName': 'eventName', 'inListFilter': {'values': kev}}}
        reqs.append({'dateRanges': [{'startDate': d0, 'endDate': d1}],
                     'dimensions': [{'name': 'date'}, {'name': 'eventName'}],
                     'metrics': [{'name': 'eventCount'}],
                     'dimensionFilter': {'andGroup': {'expressions': [f_ads, f_ev]}}, 'limit': 5000})
    res = ga4(prop, reqs)
    idx = pd.date_range(d0, d1)
    G = pd.DataFrame(index=idx)
    s = res[0]
    if not s.empty:
        G['ga_sesiones_ads'] = s.assign(date=pd.to_datetime(s.date)).set_index('date').sessions.reindex(idx).fillna(0)
    ch = res[1]
    if not ch.empty:
        ch = ch.assign(date=pd.to_datetime(ch.date))
        org = ch[ch.sessionDefaultChannelGroup.isin(['Organic Search', 'Direct'])].groupby('date').sessions.sum()
        G['ga_organico_directo'] = org.reindex(idx).fillna(0)
    eventos = []
    if len(res) > 2 and not res[2].empty:
        ev = res[2].assign(date=pd.to_datetime(res[2].date))
        for e_, g in ev.groupby('eventName'):
            G['ga::' + e_] = g.set_index('date').eventCount.reindex(idx).fillna(0)
            eventos.append('ga::' + e_)
    return G, eventos


def validar_ga4(S, G, d1):
    """La propiedad es la correcta si recibe sesiones atribuidas a ESTE ID de cliente de Ads
    (≥ 20 % de los clics de las últimas 4 semanas; el modo de consentimiento reduce la ratio)."""
    if 'ga_sesiones_ads' not in G:
        return False, 0.0
    w = slice(pd.Timestamp(d1) - pd.Timedelta(days=27), pd.Timestamp(d1))
    clics = S.loc[w, 'clicks'].sum()
    ratio = G.loc[w, 'ga_sesiones_ads'].sum() / clics if clics else 0
    return ratio >= 0.2, round(float(ratio), 2)


# ---------------------------------------------------------------- estadística
def _phi(res):
    res = [r for r in res if np.isfinite(r)]
    return max(1.0, float(np.mean(np.square(res)))) if len(res) >= 10 else 1.0


def _nb_cdf(k, lam, phi):
    if phi <= 1.0001:
        return float(stats.poisson.cdf(k, lam))
    return float(stats.nbinom.cdf(k, lam / (phi - 1), 1 / phi))


def _nb_sf(k, lam, phi):
    if phi <= 1.0001:
        return float(stats.poisson.sf(k - 1, lam))
    return float(stats.nbinom.sf(k - 1, lam / (phi - 1), 1 / phi))


def evaluar(S, senal, w_start, w_end, last_day, min_lam=5, min_diff=5, k=0.5, h=5.0, agg=3):
    """Devuelve lista de alertas (dicts) para una señal (nombre, numerador, denominador)."""
    name, y, x = senal['nombre'], senal['y'], senal.get('x')
    ref = S.loc[w_start - pd.Timedelta(days=REF_DAYS): w_start - pd.Timedelta(days=1)]
    win = S.loc[w_start: w_end]
    if y not in S or (x and x not in S) or ref[y].sum() < 10:
        return [], None
    dow = ref.index.dayofweek
    if x:
        rate = {d: ref[y][dow == d].sum() / max(ref[x][dow == d].sum(), 1e-9) for d in range(7)}
    else:
        rate = {d: ref[y][dow == d].median() for d in range(7)}

    def lam_of(D, d):
        return (D.at[d, x] * rate[d.dayofweek]) if x else rate[d.dayofweek]

    lr = np.array([lam_of(ref, d) for d in ref.index])
    yr = ref[y].values
    ok = lr > 0
    phi = _phi(list(((yr - lr) / np.sqrt(np.maximum(lr, 1e-9)))[ok]))
    L = np.log((yr[ok] + 0.5) / (lr[ok] + 0.5))
    mu = float(np.median(L)) if len(L) else 0.0
    sig = max(1.4826 * float(np.median(np.abs(L - mu))) if len(L) else 0.5, 0.05)

    out, cl, run_lam, run_k, hist = [], 0.0, 0.0, 0, []
    cus_o = cus_l = 0.0
    for d in win.index:
        lam = lam_of(win, d)
        if lam <= 0:
            continue
        obs = int(round(win.at[d, y]))
        z = (math.log((obs + 0.5) / (lam + 0.5)) - mu) / sig
        prov = d == last_day
        base = {'fecha': d.date().isoformat(), 'senal': name, 'obs': obs, 'esperado': round(lam, 1),
                'provisional': bool(prov), 'familia': senal['familia']}
        hist.append((obs, lam))
        if lam >= min_lam and obs / lam <= 0.5 and z <= -3 and _nb_cdf(obs, lam, phi) < 0.001:
            out.append(base | {'tipo': 'CAÍDA'})
        if obs / lam >= 2 and obs - lam >= min_diff and z >= 3 and _nb_sf(obs, lam, phi) < 0.001:
            out.append(base | {'tipo': 'SUBIDA'})
        if len(hist) >= agg:
            O = sum(o for o, _ in hist[-agg:])
            Lm = sum(l_ for _, l_ in hist[-agg:])
            if Lm >= 8 and O / Lm <= 0.5 and _nb_cdf(O, Lm, phi) < 0.001:
                out.append(base | {'tipo': 'CAÍDA 3 días', 'obs': O, 'esperado': round(Lm, 1)})
            if O / Lm >= 2 and O - Lm >= 8 and _nb_sf(O, Lm, phi) < 0.001:
                out.append(base | {'tipo': 'SUBIDA 3 días', 'obs': O, 'esperado': round(Lm, 1)})
        if obs == 0:
            run_lam += lam
            run_k += 1
        else:
            run_lam, run_k = 0.0, 0
        if run_k >= 2 and _nb_cdf(0, run_lam, phi) < 0.001:
            out.append(base | {'tipo': f'CEROS {run_k} días', 'obs': 0, 'esperado': round(run_lam, 1)})
        # CUSUM inferior con tamaño de efecto
        if cl == 0:
            cus_o = cus_l = 0.0
        cl = max(0.0, cl - z - k)
        if cl > 0:
            cus_o += obs
            cus_l += lam
        if cl > h:
            if cus_l and cus_o / cus_l <= 0.7:
                out.append(base | {'tipo': 'DEGRADACIÓN', 'obs': int(cus_o), 'esperado': round(cus_l, 1)})
            cl = 0.0
    return out, {'phi': round(phi, 2), 'sigma_log': round(sig, 3)}


# ---------------------------------------------------------------- señales
def construir_senales(S, acciones, ga_eventos, ga_ok):
    sen = [{'nombre': 'Clics (tráfico)', 'y': 'clicks', 'x': None, 'familia': 'trafico'},
           {'nombre': 'Impresiones', 'y': 'impr', 'x': None, 'familia': 'trafico'}]
    ref_tail = S.iloc[-(REF_DAYS + WIN_DAYS):-WIN_DAYS] if len(S) > REF_DAYS + WIN_DAYS else S
    vol = {c: ref_tail[c].sum() / max(len(ref_tail), 1) for c in acciones}
    sel = sorted([c for c in acciones if vol[c] >= 1.5], key=lambda c: -vol[c])[:8]
    for c in sel:
        a = acciones[c]
        sen.append({'nombre': f"{a['nombre']} / clics", 'y': c, 'x': 'clicks', 'familia': a['clase']})
    web = [c for c in sel if acciones[c]['clase'] == 'web']
    llam = [c for c in sel if acciones[c]['clase'] == 'llamada']
    # control: web frente a llamadas (las llamadas no dependen de la web)
    lead_web = [c for c in web if acciones[c]['categoria'] in ('SUBMIT_LEAD_FORM', 'CONTACT', 'QUALIFIED_LEAD')]
    if lead_web and llam:
        sen.append({'nombre': f"{acciones[lead_web[0]]['nombre']} vs llamadas", 'y': lead_web[0],
                    'x': llam[0], 'familia': 'control_web'})
    # coherencia entre etiquetas (Ads vs importada de GA) de la misma categoría
    for cat in set(acciones[c]['categoria'] for c in web):
        fam = {}
        for c in web:
            if acciones[c]['categoria'] == cat:
                fam.setdefault(acciones[c]['familia'], c)
        if 'ads' in fam and 'ga' in fam:
            sen.append({'nombre': f"{acciones[fam['ads']]['nombre']} vs {acciones[fam['ga']]['nombre']}",
                        'y': fam['ads'], 'x': fam['ga'], 'familia': 'coherencia'})
    # embudo e-commerce: cada paso frente al anterior
    por_paso = {}
    for c in web:
        cat = acciones[c]['categoria']
        if cat in FUNNEL and cat not in por_paso:
            por_paso[cat] = c
    pasos = [p for p in FUNNEL if p in por_paso]
    for a_, b_ in zip(pasos, pasos[1:]):
        sen.append({'nombre': f"Embudo {acciones[por_paso[b_]]['nombre']} / {acciones[por_paso[a_]]['nombre']}",
                    'y': por_paso[b_], 'x': por_paso[a_], 'familia': 'embudo'})
    if ga_ok:
        sen.append({'nombre': 'Web carga (sesiones GA4 / clics)', 'y': 'ga_sesiones_ads', 'x': 'clicks',
                    'familia': 'web_carga'})
        sen.append({'nombre': 'Orgánico + directo GA4 (control web)', 'y': 'ga_organico_directo', 'x': None,
                    'familia': 'web_global'})
        gvol = {e: ref_tail[e].sum() / max(len(ref_tail), 1) for e in ga_eventos if e in ref_tail}
        for e in sorted([e for e in gvol if gvol[e] >= 1.5], key=lambda e: -gvol[e])[:4]:
            sen.append({'nombre': f"GA4 {e[4:]} / sesiones", 'y': e, 'x': 'ga_sesiones_ads', 'familia': 'ga_evento'})
        GA_CAT = {'generate_lead': 'SUBMIT_LEAD_FORM', 'form_submit': 'SUBMIT_LEAD_FORM',
                  'purchase': 'PURCHASE', 'add_to_cart': 'ADD_TO_CART', 'begin_checkout': 'BEGIN_CHECKOUT'}
        hechos = set()
        for e in sorted([e for e in gvol if gvol[e] >= 1.5], key=lambda e: -gvol[e]):
            cat = GA_CAT.get(e[4:])
            cand = [c for c in web if acciones[c]['familia'] == 'ads' and acciones[c]['categoria'] == cat]
            if cat and cand and cat not in hechos:
                hechos.add(cat)
                sen.append({'nombre': f"{acciones[cand[0]]['nombre']} (Ads) vs GA4 {e[4:]}",
                            'y': cand[0], 'x': e, 'familia': 'coherencia'})
    return sen


# ---------------------------------------------------------------- URLs
def _norm(u):
    return re.sub(r'\{ignore\}', '', u or '').split('?')[0].rstrip('/')


ERROR_TITULO = re.compile(r'404|no encontrad|not found|error|mantenimiento|maintenance|unavailable|no disponible', re.I)


def revisar_urls(acc, d1, estado_previo):
    """Conversiones por URL (7 días, sin el último) + visita activa a las URLs principales."""
    d1 = pd.Timestamp(d1)
    w1, w0 = d1 - pd.Timedelta(days=1), d1 - pd.Timedelta(days=7)
    r0 = w0 - pd.Timedelta(days=28)
    q = (f"SELECT segments.date, landing_page_view.unexpanded_final_url, metrics.clicks, metrics.conversions "
         f"FROM landing_page_view WHERE segments.date BETWEEN '{r0.date()}' AND '{w1.date()}' AND metrics.clicks > 0")
    lp = pd.DataFrame([{'date': r['segments']['date'], 'url': _norm(r['landingPageView'].get('unexpandedFinalUrl')),
                        'clicks': num(r['metrics'].get('clicks')), 'conv': num(r['metrics'].get('conversions'))}
                       for r in gaql(acc, q)])
    alertas, urls = [], []
    if not lp.empty:
        lp['date'] = pd.to_datetime(lp.date)
        ref, win = lp[lp.date < w0], lp[lp.date >= w0]
        acc_rate = ref.conv.sum() / max(ref.clicks.sum(), 1)
        R = ref.groupby('url')[['clicks', 'conv']].sum()
        W = win.groupby('url')[['clicks', 'conv']].sum()
        D = W.join(R, rsuffix='_ref', how='left').fillna(0)
        D['lam'] = D.clicks * (D.conv_ref + 200 * acc_rate) / (D.clicks_ref + 200)  # contracción hacia la cuenta
        D = D.sort_values('clicks', ascending=False)
        for u, r in D.iterrows():
            if r.lam >= 5 and (r.conv == 0 or (r.conv / r.lam <= 0.5 and stats.poisson.cdf(r.conv, r.lam) < 0.001)):
                alertas.append({'fecha': w1.date().isoformat(), 'senal': f'URL {u}', 'tipo': 'CEROS 7 días' if r.conv == 0 else 'CAÍDA 7 días',
                                'obs': round(r.conv, 1), 'esperado': round(r.lam, 1), 'provisional': False, 'familia': 'url'})
        cum = (D.clicks.cumsum() / D.clicks.sum())
        urls = list(D.index[: max(1, int((cum < 0.8).sum()) + 1)][:8])
        urls += [a['senal'][4:] for a in alertas if a['senal'][4:] not in urls]
    visitas = {}
    for u in urls:
        try:
            t0 = time.time()
            r = requests.get(u, timeout=25, headers={'User-Agent': 'Mozilla/5.0 (vigilancia-landings)'})
            seg = round(time.time() - t0, 2)
            html = r.text or ''
            m = re.search(r'<title[^>]*>(.*?)</title>', html, re.I | re.S)
            titulo = (m.group(1).strip() if m else '')[:120]
            form = bool(re.search(r'<form|hs-form|hbspt|typeform|wpcf7|gform|<input[^>]+type=["\']email', html, re.I))
            visitas[u] = {'status': r.status_code, 'segundos': seg, 'formulario': form, 'titulo': titulo}
        except Exception as ex:
            visitas[u] = {'status': 0, 'segundos': None, 'formulario': False, 'titulo': '', 'error': str(ex)[:120]}
    hoy = d1.date().isoformat()
    for u, v in visitas.items():
        prev = (estado_previo or {}).get(u, {})
        base = {'fecha': hoy, 'senal': f'Visita {u}', 'provisional': False, 'familia': 'visita', 'obs': None, 'esperado': None}
        if v['status'] == 0 or v['status'] >= 400:
            alertas.append(base | {'tipo': f"NO RESPONDE ({v.get('status') or v.get('error', '')})"})
        elif ERROR_TITULO.search(v['titulo'] or '') and not ERROR_TITULO.search(prev.get('titulo', '') or ''):
            alertas.append(base | {'tipo': f"PÁGINA DE ERROR («{v['titulo'][:50]}»)"})
        elif v['segundos'] and v['segundos'] > 8:
            alertas.append(base | {'tipo': f"LENTA ({v['segundos']} s)"})
        elif prev.get('formulario') and not v['formulario']:
            alertas.append(base | {'tipo': 'FORMULARIO DESAPARECIDO'})
    return alertas, visitas


def revisar_directas(acc):
    """Anuncios rechazados por destino (web caída o URL rota) en campañas CON tráfico en 7 días.
    Los de campañas o experimentos sin impresiones son higiene y no alertan."""
    al = []
    try:
        activas = {r['campaign']['id'] for r in gaql(acc, "SELECT campaign.id, metrics.impressions FROM campaign "
                                                          "WHERE segments.date DURING LAST_7_DAYS AND metrics.impressions > 0")}
        q = ("SELECT campaign.id, ad_group_ad.ad.id, ad_group_ad.ad.final_urls, ad_group_ad.policy_summary.approval_status, "
             "ad_group_ad.policy_summary.policy_topic_entries FROM ad_group_ad "
             "WHERE ad_group_ad.status = 'ENABLED' AND campaign.status = 'ENABLED' "
             "AND ad_group_ad.policy_summary.approval_status IN ('DISAPPROVED', 'APPROVED_LIMITED')")
        n, urls = 0, set()
        for r in gaql(acc, q):
            topics = json.dumps(r.get('adGroupAd', {}).get('policySummary', {}).get('policyTopicEntries', []))
            if re.search(r'DESTINATION', topics, re.I) and r['campaign']['id'] in activas:
                n += 1
                urls.update(r['adGroupAd']['ad'].get('finalUrls', []))
        if n:
            al.append({'fecha': dt.date.today().isoformat(), 'senal': 'Anuncios rechazados por destino',
                       'tipo': f"{n} anuncios ({', '.join(sorted(urls))[:120]})", 'obs': n, 'esperado': 0,
                       'provisional': False, 'familia': 'politica'})
    except Exception:
        pass
    return al


def cambios_recientes(acc, d1, dias=10):
    """Cambios de presupuesto, estado de campaña o estrategia de puja en los últimos días."""
    desde = (pd.Timestamp(d1) - pd.Timedelta(days=dias)).date().isoformat()
    hasta = (pd.Timestamp(d1) + pd.Timedelta(days=1)).date().isoformat()
    q = ("SELECT change_event.change_date_time, change_event.change_resource_type, change_event.changed_fields "
         "FROM change_event WHERE change_event.change_date_time >= '" + desde + "' "
         "AND change_event.change_date_time <= '" + hasta + "' "
         "AND change_event.change_resource_type IN ('CAMPAIGN_BUDGET', 'CAMPAIGN') "
         "ORDER BY change_event.change_date_time DESC LIMIT 500")
    try:
        rows = gaql(acc, q)
    except Exception:
        return []
    out = set()
    for r in rows:
        ce = r.get('changeEvent', {})
        campos = str(ce.get('changedFields', ''))
        if ce.get('changeResourceType') == 'CAMPAIGN' and not re.search(r'status|bidding|targetCpa|targetRoas|maximize', campos, re.I):
            continue
        out.add(ce.get('changeDateTime', '')[:10])
    return sorted(out)


def clasificar(a, ga_ok):
    t, fam = a['tipo'], a['familia']
    if a['provisional']:
        return '⚪', 'Provisional (ayer sin procesar): re-verificar mañana'
    sube = t.startswith('SUBIDA')
    if fam == 'politica':
        return '🔴', 'Anuncios rechazados por destino: web caída o con error'
    if fam == 'visita':
        return ('🟠' if t.startswith(('LENTA', 'FORMULARIO')) else '🔴'), f'Landing: {t.lower()}'
    if fam == 'url':
        return '🟠', 'Landing sin conversiones: probar formulario de esa URL'
    if fam in ('web_carga', 'web_global'):
        return ('🔴', 'Web caída o lenta (llegan clics, no sesiones)') if not sube else ('🟡', 'Subida de sesiones atípica')
    if fam == 'trafico':
        if sube:
            return '🟡', 'Subida de tráfico atípica'
        if a.get('esperado') and a['obs'] is not None and a['obs'] <= 0.1 * a['esperado']:
            return '🔴', 'Cuenta casi parada: facturación, rechazos o pausas'
        if a.get('cambios'):
            return '🟡', f"Caída de tráfico tras cambios de presupuesto/campañas ({', '.join(d[5:] for d in a['cambios'][-3:])}): probablemente intencionada"
        return '🟠', 'Caída de tráfico sin cambios en la cuenta: revisar estado de campañas, pujas y facturación'
    if fam == 'coherencia':
        return ('🔴', 'Una etiqueta no registra y la otra sí: etiqueta rota') if not sube else ('🟠', 'Una etiqueta registra de más: posible duplicado')
    if fam == 'control_web':
        return ('🔴', 'Formularios caen y llamadas no: problema en la web') if not sube else ('🟡', 'Formularios suben frente a llamadas')
    if fam == 'embudo':
        return ('🔴', 'Rotura en un paso del embudo (checkout/pago/etiqueta)') if not sube else ('🟠', 'Paso del embudo registra de más')
    if fam == 'ga_evento':
        return ('🔴', 'GA4 deja de registrar la conversión (formulario o GTM)') if not sube else ('🟠', 'GA4 registra de más: duplicado o spam')
    # acciones de conversión / clics
    if sube:
        return '🟠', 'Conversión x2 o más: posible duplicado o spam'
    if t.startswith('DEGRADACIÓN'):
        return '🟠', 'Caída sostenida: web lenta o fallo parcial'
    return '🔴', 'Conversión hundida con tráfico normal: formulario o etiqueta'


ORDEN = {'🔴': 0, '🟠': 1, '🟡': 2, '⚪': 3, '🟢': 4}


# ---------------------------------------------------------------- orquestación
def revisar_cuenta(acc, d1=None):
    d1 = pd.Timestamp(d1 or (dt.date.today() - dt.timedelta(days=1)))
    d0 = d1 - pd.Timedelta(days=REF_DAYS + WIN_DAYS + 7)
    S, acciones = descargar(acc, d0.date().isoformat(), d1.date().isoformat())
    ga_ok, ga_ratio, ga_eventos = False, None, []
    if acc.get('ga4'):
        try:
            G, ga_eventos = descargar_ga4(acc, d0.date().isoformat(), d1.date().isoformat())
            ga_ok, ga_ratio = validar_ga4(S, G, d1)
            if ga_ok:
                S = S.join(G)
        except Exception as ex:
            ga_ratio = f'error: {str(ex)[:80]}'
    senales = construir_senales(S, acciones, ga_eventos, ga_ok)
    w_start = d1 - pd.Timedelta(days=WIN_DAYS - 1)
    alertas, meta = [], {}
    for s in senales:
        al, m = evaluar(S, s, w_start, d1, last_day=d1)
        alertas += al
        if m:
            meta[s['nombre']] = m
    # solo lo reciente: últimos RECENT_DAYS días (el último, provisional)
    lim = (d1 - pd.Timedelta(days=RECENT_DAYS - 1)).date().isoformat()
    alertas = [a for a in alertas if a['fecha'] >= lim]
    # las roturas en el último día solo cuentan como firmes si ya venían del día anterior
    prev_path = HIST_DIR + f"{acc['id']}_estado.json"
    try:
        prev = json.load(open(prev_path))
    except Exception:
        prev = {}
    al_url, visitas = revisar_urls(acc, d1, prev.get('visitas'))
    alertas += al_url + revisar_directas(acc)
    if any(a['familia'] == 'trafico' and not a['tipo'].startswith('SUBIDA') for a in alertas):
        cambios = cambios_recientes(acc, d1)
        for a in alertas:
            if a['familia'] == 'trafico':
                a['cambios'] = cambios
    for a in alertas:
        a['estado'], a['lectura'] = clasificar(a, ga_ok)
    # una fila por señal: la más grave
    best = {}
    for a in sorted(alertas, key=lambda a: (ORDEN[a['estado']], a['fecha'])):
        best.setdefault(a['senal'], a)
    filas = sorted(best.values(), key=lambda a: ORDEN[a['estado']])
    estado = filas[0]['estado'] if filas else '🟢'
    res = {'cuenta': acc['nombre'], 'id': acc['id'], 'fecha_datos': d1.date().isoformat(),
           'estado': estado, 'filas': filas, 'n_senales': len(senales) + len(visitas),
           'ga4': {'propiedad': acc.get('ga4'), 'valida': ga_ok, 'sesiones_por_clic': ga_ratio},
           'senales': [s['nombre'] for s in senales], 'meta': meta, 'visitas': visitas}
    try:
        import os
        os.makedirs(HIST_DIR, exist_ok=True)
        json.dump({'visitas': visitas, 'fecha': d1.date().isoformat()}, open(prev_path, 'w'), ensure_ascii=False)
        json.dump(res, open(HIST_DIR + f"{acc['id']}_{d1.date().isoformat()}.json", 'w'), ensure_ascii=False, default=str)
    except Exception:
        pass
    return res


def _fmt_fila(a):
    o = '' if a['obs'] is None else f"{a['obs']} vs {a['esperado']}"
    return f"| {a['senal']} | {a['estado']} | {a['fecha'][5:]} · {a['tipo'].lower()} | {o} | {a['lectura']} |"


def validar_config(cuentas):
    """Devuelve la lista de problemas de configuración (vacía si es válida)."""
    if not isinstance(cuentas, list) or not cuentas:
        return ['no hay ninguna cuenta configurada']
    prob = []
    for i, c in enumerate(cuentas):
        if not isinstance(c, dict):
            prob.append(f'entrada {i + 1}: no es un objeto JSON')
            continue
        nom = c.get('nombre') or c.get('id') or f'entrada {i + 1}'
        if not c.get('id'):
            prob.append(f'{nom}: falta "id"')
        if c.get('estado', 'activa') != 'activa':
            continue
        cid = str(c.get('customer_id') or '').replace('-', '')
        if not re.fullmatch(r'\d{10}', cid):
            prob.append(f'{nom}: "customer_id" debe ser el ID de Google Ads de 10 dígitos')
        a = c.get('acceso') or {}
        con, mcc = str(a.get('conexion') or ''), str(a.get('login_customer_id') or '').replace('-', '')
        if not con and not mcc:
            prob.append(f'{nom}: falta "acceso" ("conexion" de Composio o "login_customer_id" de la MCC)')
        if '<' in con or (mcc and not re.fullmatch(r'\d{10}', mcc)):
            prob.append(f'{nom}: "acceso" tiene un valor de plantilla o no válido')
        g = c.get('ga4')
        if g and not re.fullmatch(r'properties/\d+', str(g)):
            prob.append(f'{nom}: "ga4" debe ser properties/<número> o null')
    if not prob and not any(c.get('estado', 'activa') == 'activa' for c in cuentas):
        prob.append('no hay ninguna cuenta con "estado": "activa"')
    return prob


def informe_diario(ids=None, d1=None, cuentas=None):
    from concurrent.futures import ThreadPoolExecutor
    import os
    if cuentas is None:
        try:
            cuentas = json.load(open(CONFIG_PATH))
        except FileNotFoundError:
            cuentas = None
        except Exception as ex:
            return (f"⚠️ Vigilancia no ejecutada: {CONFIG_PATH} no se puede leer ({str(ex)[:120]}). "
                    "Revisa el formato con references/cuentas.plantilla.json."), []
        if cuentas is None:
            return ("⚠️ Vigilancia no ejecutada: falta configurar las cuentas. Indica al menos el ID de Google Ads "
                    "de una cuenta y su acceso (ver 'Configuración inicial' en la skill)."), []
    prob = validar_config(cuentas)
    if prob:
        return ("⚠️ Vigilancia no ejecutada: configuración de cuentas incompleta. "
                + '; '.join(prob[:6]) + ". Ver 'Configuración inicial' en la skill."), []
    for c in cuentas:
        if c.get('customer_id'):
            c['customer_id'] = str(c['customer_id']).replace('-', '')
        if (c.get('acceso') or {}).get('login_customer_id'):
            c['acceso']['login_customer_id'] = str(c['acceso']['login_customer_id']).replace('-', '')
    try:
        os.makedirs(BASE, exist_ok=True)
        json.dump(cuentas, open(CONFIG_PATH, 'w'), ensure_ascii=False, indent=1)
    except Exception:
        pass
    activas = [c for c in cuentas if c.get('estado', 'activa') == 'activa' and (not ids or c['id'] in ids)]
    pendientes = [c for c in cuentas if c.get('estado') == 'pendiente' and (not ids or c['id'] in ids)]

    def run(c):
        try:
            return revisar_cuenta(c, d1)
        except Exception as ex:
            return {'cuenta': c['nombre'], 'id': c['id'], 'estado': '⚠️', 'error': str(ex)[:200], 'filas': []}

    # los helpers de Composio imprimen trazas: se silencian UNA vez, fuera de los hilos
    with contextlib.redirect_stdout(io.StringIO()):
        with ThreadPoolExecutor(max_workers=4) as ex:
            res = list(ex.map(run, activas))
    fecha = next((r.get('fecha_datos') for r in res if r.get('fecha_datos')), '')
    L = [f"**Vigilancia de roturas — datos hasta {fecha}**", '',
         '| Cuenta | Estado | Resumen |', '|---|---|---|']
    for r in sorted(res, key=lambda r: ORDEN.get(r['estado'], -1)):
        if r.get('error'):
            L.append(f"| {r['cuenta']} | ⚠️ | No se pudo revisar: {r['error'][:90]} |")
            continue
        firmes = [a for a in r['filas'] if a['estado'] in ('🔴', '🟠')]
        avisos = [a for a in r['filas'] if a['estado'] == '🟡']
        ga = 'con GA4' if r['ga4']['valida'] else ('GA4 no válida' if r['ga4']['propiedad'] else 'sin GA4')
        resumen = (f"{len(firmes)} alerta(s)" if firmes else (f"{len(avisos)} aviso(s)" if avisos else f"{r['n_senales']} señales OK")) + f" · {ga}"
        L.append(f"| {r['cuenta']} | {r['estado']} | {resumen} |")
    for c in pendientes:
        L.append(f"| {c['nombre']} | ⚪ | Pendiente de configurar |")
    det = [r for r in res if any(a['estado'] in ('🔴', '🟠', '🟡', '⚪') for a in r.get('filas', []))]
    for r in det:
        L += ['', f"**{r['cuenta']}**", '', '| Señal | Estado | Cuándo | Obs. vs esperado | Lectura |', '|---|---|---|---|---|']
        L += [_fmt_fila(a) for a in r['filas'] if a['estado'] in ('🔴', '🟠', '🟡', '⚪')][:6]
    return '\n'.join(L), res
