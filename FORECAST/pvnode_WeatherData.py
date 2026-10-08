import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import requests
import json
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import FUNCTIONS.functions
import FUNCTIONS.WeatherData

# Prognosedaten von https://pvnode.com holen
# API V2: Standort (site) wird in der pvnode Web-App konfiguriert, Abfrage über site_id
# API V1: Standort und Ausrichtung kommen aus [pv.strings] der weather.ini
# pvnode liefert 15-Minuten-Werte (mittlere Leistung des Intervalls, das am Zeitstempel endet).

API_URL = 'https://api.pvnode.com'
# Hier wird next_poll_at der letzten V2-Abfrage gespeichert (relativ zum GEN24-Verzeichnis)
POLL_STATUS_DATEI = 'pvnode_poll.json'

def _fetch_pvnode(url, params, label):
    """
    Ruft eine pvnode-URL mit Bearer-Token ab.
    Rückgabe: (json_daten, None) bei Erfolg, (None, Fehlermeldung) bei Fehler.
    """
    headers = {'Authorization': 'Bearer ' + api_key}
    try:
        apiResponse = requests.get(url, params=params, headers=headers, timeout=52.50)
        print("DEBUG Wetter URL ("+label+"): "+apiResponse.url)
        if apiResponse.status_code == 200:
            return apiResponse.json(), None
        elif apiResponse.status_code in (401, 403):
            print(f"### ERROR {apiResponse.status_code}: Zugriff von api.pvnode.com verweigert ({label}). API-Key, site_id bzw. Standort prüfen.\n### ERROR Response Body: {apiResponse.text}")
        elif apiResponse.status_code == 429:
            print(f"### ERROR 429: Abfragelimit von api.pvnode.com erreicht ({label}).")
        else:
            print(f"### ERROR {apiResponse.status_code}: Keine forecasts-Daten von api.pvnode.com ({label})\n### ERROR Response Body: {apiResponse.text}")
        return None, "Fehler "+str(apiResponse.status_code)+" ("+label+")"
    except requests.exceptions.Timeout:
        print("### ERROR:  Timeout von api.pvnode.com ("+label+")")
        return None, "Timeout ("+label+")"
    except (requests.exceptions.RequestException, ValueError) as error:
        print("### ERROR:  Anfrage an api.pvnode.com fehlgeschlagen ("+label+"): "+str(error))
        return None, str(error)

def _parse_utc(zeit_str):
    # pvnode liefert UTC, V1 als "YYYY-MM-DD HH:MM:SS", V2 als ISO-Zeitstempel
    zeit = datetime.fromisoformat(zeit_str.strip().replace(' ', 'T').replace('Z', '+00:00'))
    if zeit.tzinfo is None:
        zeit = zeit.replace(tzinfo=timezone.utc)
    return zeit

def _viertelstunden_zu_stunden(werte):
    """
    werte: Liste von (utc_datetime, watt) der 15-Minuten-Intervalle (Wert gilt für das Intervall, das am Zeitstempel endet).
    Ergebnis: dict {'YYYY-MM-DD HH:00:00' (lokale Zeit): Watt}
    Für jede volle Stunde wird der Mittelwert der vier Intervalle von HH-30min bis HH+30min gebildet,
    damit entspricht der Wert der mittleren Leistung um die volle Stunde.
    """
    stunden = defaultdict(list)
    for zeit, watt in werte:
        # Intervallmitte bestimmen und der nächstgelegenen vollen Stunde zuordnen
        mitte = zeit - timedelta(minutes=7, seconds=30)
        stunde = (mitte + timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0)
        stunden[stunde].append(watt)

    dict_watts = {}
    for stunde, watt_liste in stunden.items():
        key = stunde.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        dict_watts[key] = int(round(sum(watt_liste) / len(watt_liste)))
    return dict_watts

def _sql_daten(dict_watts, Quelle, Gewicht):
    SQL_watts = []
    for key, value in sorted(dict_watts.items()):
        if (value > 10):
            SQL_watts.append((key, Quelle, value, Gewicht, ''))
    return SQL_watts

def _lese_poll_status(site_id):
    # Liefert next_poll_at (UTC) der letzten Abfrage für diese site_id und forecast_days, sonst None
    try:
        with open(POLL_STATUS_DATEI, 'r') as datei:
            status = json.load(datei)
        if status.get('site_id') == site_id and status.get('forecast_days') == forecast_days:
            return _parse_utc(status['next_poll_at'])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None

def _schreibe_poll_status(site_id, json_data):
    # computed_at/next_poll_at liefert pvnode ohne Zeitzone, wegen timezone=utc als UTC behandelt
    if not json_data.get('next_poll_at'):
        return
    try:
        with open(POLL_STATUS_DATEI, 'w') as datei:
            json.dump({'site_id': site_id, 'forecast_days': forecast_days,
                       'computed_at': json_data.get('computed_at'),
                       'next_poll_at': json_data['next_poll_at']}, datei)
    except OSError as error:
        print("### WARNUNG: "+POLL_STATUS_DATEI+" konnte nicht geschrieben werden: "+str(error))

def loadLatestWeatherData_v2(Quelle, Gewicht):
    site_id = basics.getVarConf('pvnode', 'site_id', 'str')

    # Vor next_poll_at liefert pvnode nur die gleiche Prognose aus dem Cache => Abfrage sparen
    next_poll_at = _lese_poll_status(site_id)
    if next_poll_at is not None and datetime.now(timezone.utc) < next_poll_at and not erzwingen:
        return ([], "Keine neue Prognose vor "+next_poll_at.astimezone().strftime("%Y-%m-%d %H:%M")+", Abfrage übersprungen.")

    url = '{}/v2/forecast/{}'.format(API_URL, site_id)
    params = {'forecast_days': forecast_days, 'past_days': 0, 'timezone': 'utc'}
    json_data, fehler = _fetch_pvnode(url, params, 'site '+site_id)
    if json_data is None:
        return (None, fehler)
    _schreibe_poll_status(site_id, json_data)

    try:
        werte = [(_parse_utc(wert['timestamp']), float(wert['pv_power'] or 0)) for wert in json_data['values']]
    except (KeyError, TypeError, ValueError) as error:
        return (None, "Unerwartetes Datenformat von api.pvnode.com: "+str(error))

    return (_sql_daten(_viertelstunden_zu_stunden(werte), Quelle, Gewicht), None)

def loadLatestWeatherData_v1(Quelle, Gewicht):
    anzahl_strings = basics.getVarConf('pv.strings', 'anzahl', 'eval')
    lat = basics.getVarConf('pv.strings', 'lat', 'eval')
    lon = basics.getVarConf('pv.strings', 'lon', 'eval')
    url = API_URL + '/v1/forecast/'

    SQL_watts_dict = {}
    for string_zaehler in range(1, anzahl_strings + 1):
        suffix = '' if string_zaehler == 1 else string_zaehler
        dec = basics.getVarConf('pv.strings', f'dec{suffix}', 'eval')
        az = basics.getVarConf('pv.strings', f'az{suffix}', 'eval')
        wp = basics.getVarConf('pv.strings', f'wp{suffix}', 'eval')
        # weather.ini: 0 = Süden, -90 = Osten; pvnode: 0 = Norden, 90 = Osten, 180 = Süden
        orientation = (az + 180) % 360
        params = {
            'latitude': lat,
            'longitude': lon,
            'slope': dec,
            'orientation': orientation,
            'pv_power_kw': wp / 1000,
            'required_data': 'pv_watts',
            'forecast_days': forecast_days,
            'past_days': 0,
        }
        json_data, fehler = _fetch_pvnode(url, params, 'String'+str(string_zaehler))
        if json_data is None:
            return (None, fehler)

        try:
            werte = [(_parse_utc(wert['dtm']), float(wert['pv_watts'] or 0)) for wert in json_data['values']]
        except (KeyError, TypeError, ValueError) as error:
            return (None, "Unerwartetes Datenformat von api.pvnode.com: "+str(error))

        SQL_watts_dict[string_zaehler] = _sql_daten(_viertelstunden_zu_stunden(werte), Quelle, Gewicht)

    # Prognosen mehrerer Strings addieren
    return (weatherdata.sum_pv_data(SQL_watts_dict), None)


if __name__ == '__main__':
    basics = FUNCTIONS.functions.basics()
    weatherdata = FUNCTIONS.WeatherData.WeatherData()
    config = basics.loadConfig(['default', 'weather'])
    ForecastCalcMethod = basics.getVarConf('env','ForecastCalcMethod','str')
    api_key = basics.getVarConf('pvnode', 'api_key', 'str')
    api_version = basics.getVarConf('pvnode', 'api_version', 'eval')
    forecast_days = basics.getVarConf('pvnode', 'forecast_days', 'eval')
    offset_minuten = basics.getVarConf('pvnode', 'offset_minuten', 'eval')
    Gewicht = basics.getVarConf('pvnode', 'Gewicht', 'eval')
    Quelle = 'pvnode'
    # Mit Parameter "erzwingen" wird auch vor next_poll_at abgefragt
    erzwingen = 'erzwingen' in sys.argv[1:]

    format = "%H:%M:%S"
    now = datetime.now()

    if api_version == 1:
        data, data_err = loadLatestWeatherData_v1(Quelle, Gewicht)
    else:
        data, data_err = loadLatestWeatherData_v2(Quelle, Gewicht)

    if data == []:
        print(f'{Quelle}: {data_err}\n')
    elif isinstance(data, list):
        weatherdata.storeWeatherData_SQL(data, Quelle, Gewicht, '', offset_minuten)
        # Ergebnis mit ForecastCalcMethod berechnen und in DB speichern
        weatherdata.store_forecast_result()
        print(f'{Quelle} OK: Prognosedaten und Ergebnisse ({ForecastCalcMethod}) {now.strftime(format)} gespeichert.\n')
    else:
        print("Fehler bei Datenanforderung ", Quelle, ":")
        print(data_err)
