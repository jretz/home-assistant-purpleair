"""
Provides an API capable of communicating with the free PurpleAir service.
"""

import asyncio
import logging
import re
from datetime import datetime, timezone

from aiohttp import ClientSession

from .const import (
    AQI_BREAKPOINTS,
    JSON_PROPERTIES,
    PUBLIC_URL, PRIVATE_URL
)

_LOGGER = logging.getLogger(__name__)


class PurpleAirApi:
    """Provides the API capable of communicating with PurpleAir."""

    session: ClientSession

    def __init__(self, session: ClientSession):
        self.session = session

        self._api_issues = False
        self._nodes = {}

    def get_node_count(self):
        """Gets the number of nodes registered with the API."""
        return len(self._nodes)

    def register_node(self, node_id, hidden, key):
        """
        Registers a node with this instance. This will schedule a periodic poll against PurpleAir if
        this is the first sensor added and schedule an immediate API request after 5 seconds.
        """

        if node_id in self._nodes:
            _LOGGER.debug('detected duplicate registration: %s', node_id)
            return

        self._nodes[node_id] = {'hidden': hidden, 'key': key}
        _LOGGER.debug('registered new node: %s', node_id)

    def unregister_node(self, node_id):
        """
        Unregisters a node from this instance and removes any associated data. If this is the last
        registered node the periodic polling is shut down.
        """

        if node_id not in self._nodes:
            _LOGGER.debug('detected non-existent unregistration: %s', node_id)
            return

        del self._nodes[node_id]
        _LOGGER.debug('unregistered node: %s', node_id)

    async def update(self, now=None):
        """Main update process to query and update sensor data."""

        public_nodes = [node_id for (node_id, data) in self._nodes.items() if not data['hidden']]
        private_nodes = [node_id for (node_id, data) in self._nodes.items() if data['hidden']]

        _LOGGER.debug('public nodes: %s, private nodes: %s', public_nodes, private_nodes)

        urls = self._build_api_urls(public_nodes, private_nodes)
        results = await self._fetch_data(urls)

        nodes = build_nodes(results)

        calculate_sensor_values(nodes)

        return nodes

    def _build_api_urls(self, public_nodes, private_nodes):
        """
        Builds a list of URLs to query based off the provided public and private node lists,
        attempting to combine as many sensors in to as few API requests as possible.
        """

        urls = []
        if private_nodes:
            by_keys = {}
            for node in private_nodes:
                data = self._nodes[node]
                key = data.get('key')

                if key:
                    if key not in by_keys:
                        by_keys[key] = []

                    by_keys[key].append(node)

            used_public = False
            for key, private_nodes_for_key in by_keys.items():
                nodes = private_nodes_for_key
                if not used_public:
                    nodes += public_nodes
                    used_public = True

                urls.append(PRIVATE_URL.format(nodes='|'.join(nodes), key=key))

        elif public_nodes:
            urls = [PUBLIC_URL.format(nodes='|'.join(public_nodes))]

        return urls

    async def _fetch_data(self, urls):
        """Fetches data from the PurpleAir API endpoint."""

        if not urls:
            _LOGGER.debug('no nodes provided')
            return []

        results = []
        for url in urls:
            _LOGGER.debug('fetching url: %s', url)

            # be nice to the free API when fetching multiple URLs
            await asyncio.sleep(0.5)

            async with self.session.get(url) as response:
                if response.status != 200:
                    if not self._api_issues:
                        self._api_issues = True
                        _LOGGER.warning(
                            'PurpleAir API returned bad response (%s) for url %s. %s',
                            response.status,
                            url,
                            await response.text()
                        )

                    continue

                if self._api_issues:
                    self._api_issues = False
                    _LOGGER.info('PurpleAir API responding normally')

                json = await response.json()
                results += json['results']

        return results


def build_nodes(results):
    """
    Builds a dictionary of nodes and extracts available data from the JSON result array returned
    from the PurpleAir API.
    """

    nodes = {}
    for result in results:
        sensor = 'A' if 'ParentID' not in result else 'B'
        node_id = str(result['ID'])

        if sensor == 'A':
            nodes[node_id] = {
                'last_seen': datetime.fromtimestamp(result['LastSeen'], timezone.utc),
                'last_update': datetime.fromtimestamp(result['LastUpdateCheck'], timezone.utc),
                'device_location': result.get('DEVICE_LOCATIONTYPE', 'unknown'),
                'readings': {},
                'version': result.get('Version', 'unknown'),
                'type': result.get('Type', 'unknown'),
                'label': result.get('Label'),
                'lat': float(result.get('Lat', 0)),
                'lon': float(result.get('Lon', 0)),
                'rssi': float(result.get('RSSI', 0)),
                'adc': float(result.get('Adc', 0)),
                'uptime': int(result.get('Uptime', 0)),
            }
        else:
            node_id = str(result['ParentID'])

        readings = nodes[node_id]['readings']

        sensor_data = readings.get(sensor, {})
        for prop in JSON_PROPERTIES:
            sensor_data[prop] = result.get(prop)

        if not all(value is None for value in sensor_data.values()):
            readings[sensor] = sensor_data
        else:
            _LOGGER.debug('node %s:%s did not contain any data', node_id, sensor)

    return nodes


def calc_aqi(value, index):
    """
    Calculates the corresponding air quality index based off the available conversion data using
    the sensors current Particulate Matter 2.5 value.

    Returns an AQI between 0 and 999 or None if the sensor reading is invalid.

    See AQI_BREAKPOINTS in const.py.
    """

    if index not in AQI_BREAKPOINTS:
        _LOGGER.debug('calc_aqi requested for unknown type: %s', index)
        return None

    aqi_bp_index = AQI_BREAKPOINTS[index]
    aqi_bp = next((bp for bp in aqi_bp_index if bp.pm_low <= value <= bp.pm_high), None)

    if not aqi_bp:
        _LOGGER.debug('value %s did not fall in valid range for type %s', value, index)
        return None

    aqi_range = aqi_bp.aqi_high - aqi_bp.aqi_low
    pm_range = aqi_bp.pm_high - aqi_bp.pm_low
    aqi_c = value - aqi_bp.pm_low
    return round((aqi_range / pm_range) * aqi_c + aqi_bp.aqi_low)


def calculate_sensor_values(nodes):
    """
    Mutates the provided node dictionary in place by iterating over the raw sensor data and provides
    a normalized view and adds any calculated properties.
    """

    for node in nodes:
        readings = nodes[node]['readings']
        _LOGGER.debug('processing node %s, readings: %s', node, readings)

        if 'A' in readings and 'B' in readings:
            for prop in JSON_PROPERTIES:
                if a_reading := readings['A'].get(prop):
                    a_reading = float(a_reading)

                    if b_reading := readings['B'].get(prop):
                        b_reading = float(b_reading)
                        readings[prop] = round((a_reading + b_reading) / 2, 1)

                        confidence = 'Good' if abs(a_reading - b_reading) < 45 else 'Questionable'
                        readings[f'{prop}_confidence'] = confidence
                    else:
                        readings[prop] = round(a_reading, 1)
                        readings[f'{prop}_confidence'] = 'Single'
                else:
                    readings[prop] = None
        else:
            for prop in JSON_PROPERTIES:
                if prop in readings['A']:
                    a_reading = float(readings['A'][prop])
                    readings[prop] = round(a_reading, 1)
                    readings[f'{prop}_confidence'] = 'Good'
                else:
                    readings[prop] = None

        if pm25atm := readings.get('pm2_5_atm'):
            readings['pm2_5_atm_aqi'] = calc_aqi(pm25atm, 'pm2_5')

        _LOGGER.debug('node results %s, readings: %s', node, readings)


async def get_node_configuration(session: ClientSession, url: str):
    """
    Gets a configuration for the node at the  given PurpleAir URL. This string expects to see a URL
    in the following format:

        https://www.purpleair.com/json?key={key}&show={node_id}
        https://www.purpleair.com/sensorlist?key={key}&show={node_id}
    """

    if not re.match(r'.*purpleair.*', url, re.IGNORECASE):
        raise PurpleAirApiUrlError('Provided URL is invalid', url)

    key_match = re.match(r'.*key=(?P<key>[^&]+)', url)
    node_match = re.match(r'.*show=(?P<node_id>[^&]+)', url)

    key = key_match.group('key') if key_match else None
    node_id = node_match.group('node_id') if node_match else None

    if not key or not node_id:
        raise PurpleAirApiUrlError('Unable to get node and/or key from URL', url)

    api_url = PRIVATE_URL.format(nodes=node_id, key=key)
    _LOGGER.debug('getting node info from url %s', api_url)

    data = {}
    async with session.get(api_url) as response:
        if not response.status == 200:
            raise PurpleAirApiStatusError(api_url, response.status, await response.text())

        data = await response.json()

    results = data.get('results', [])
    if not results or len(results) == 0:
        raise PurpleAirApiError('Missing results from JSON response')

    node = results[0]
    _LOGGER.debug('got node %s', node)
    node_id = node.get('ParentID') or node['ID']
    if not node_id:
        raise PurpleAirApiError('Missing node ID or ParentID')

    node_id = str(node_id)

    config = {
        'title': node.get('Label'),
        'node_id': node_id,
        'hidden': node.get('Hidden') == 'true',
        'key': node.get('THINGSPEAK_PRIMARY_ID_READ_KEY')
    }

    _LOGGER.debug('generated config for node %s: %s', node_id, config)

    return config


class PurpleAirApiError(Exception):
    """Raised when an error with the PurpleAir APi is encountered.

    Attributes:
        message -- An explanation of the error.
    """

    def __init__(self, message: str):
        super().__init__()
        self.message = message


class PurpleAirApiUrlError(PurpleAirApiError):
    """Raised when an invalid PurpleAir URL is encountered.

    Attributes:
        message -- An explanation of the error.
        url     -- The URL that is considered invalid.
    """

    def __init__(self, message: str, url: str):
        super().__init__(message)
        self.url = url


class PurpleAirApiStatusError(PurpleAirApiError):
    """Raised when an error occurs when communicating with the PurpleAir API.

    Attributes:
        message -- Generic error message.
        url     -- The URL that caused the error.
        status  -- Status code returned from the server.
        text    -- Any data returned in the body of the error from the server.
    """

    def __init__(self, url: str, status: int, text: str):
        super().__init__('An error occurred while communicating with the PurpleAir API.')
        self.url = url
        self.status = status
        self.text = text