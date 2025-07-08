# -*- coding: utf-8 -*-

# FLEDGE_BEGIN
# See: http://fledge-iot.readthedocs.io/
# FLEDGE_END

"""Unit test for python.fledge.plugins.south.coap"""

import asyncio
import copy
import json
import pytest
import cbor2
import aiocoap.error
from aiocoap import message, numbers
from unittest.mock import call, patch

from python.fledge.plugins.south.coap import coap
from python.fledge.plugins.south.coap.coap import CoAPIngest, async_ingest, c_callback, c_ingest_ref, _DEFAULT_CONFIG as config


__author__ = "Amarendra K Sinha"
__copyright__ = "Copyright (c) 2017 OSIsoft, LLC"
__license__ = "Apache 2.0"
__version__ = "${VERSION}"


_NEW_CONFIG = {
    'plugin': {
        'description': 'Python module name of the plugin to load',
        'type': 'string',
        'default': 'coap'
    },
    'port': {
        'description': 'Port to listen on',
        'type': 'integer',
        'default': '1234',
    },
    'uri': {
        'description': 'URI to accept data on',
        'type': 'string',
        'default': 'sensor-values',
    }
}


def cleanup_plugin():
    """Properly cleanup the plugin after tests"""
    # First, try the normal plugin shutdown
    try:
        if hasattr(coap, 'aiocoap_ctx') and coap.aiocoap_ctx is not None:
            coap.plugin_shutdown(config)
    except Exception:
        pass  # Ignore shutdown errors

    # Now handle the thread cleanup that the plugin doesn't do
    if hasattr(coap, 't') and coap.t is not None:
        try:
            # Stop the event loop if it's still running
            if hasattr(coap, 'loop') and coap.loop is not None:
                try:
                    if coap.loop.is_running():
                        coap.loop.call_soon_threadsafe(coap.loop.stop)
                except RuntimeError:
                    # Loop might already be stopped
                    pass

            # Wait for the thread to finish
            if coap.t.is_alive():
                coap.t.join(timeout=2.0)

                # If thread is still alive after timeout, force cleanup
                if coap.t.is_alive():
                    try:
                        # Force thread cleanup for Python 3.8+
                        coap.t._tstate_lock = None
                        coap.t._stop()
                    except Exception:
                        pass
        except Exception:
            pass

    # Clean up the event loop if it exists
    if hasattr(coap, 'loop') and coap.loop is not None:
        try:
            if not coap.loop.is_closed():
                coap.loop.close()
        except Exception:
            pass

    # Reset all global variables to ensure clean state for next test
    coap.aiocoap_ctx = None
    coap._task = None
    coap.loop = None
    coap.t = None


def test_plugin_contract():
    # Evaluates if the plugin has all the required methods
    assert callable(getattr(coap, 'plugin_info'))
    assert callable(getattr(coap, 'plugin_init'))
    assert callable(getattr(coap, 'plugin_start'))
    assert callable(getattr(coap, 'plugin_shutdown'))
    assert callable(getattr(coap, 'plugin_reconfigure'))
    assert callable(getattr(coap, 'plugin_register_ingest'))


def test_plugin_info():
    assert coap.plugin_info() == {
        'name': 'CoAP Plugin',
        'version': '3.0.0',
        'mode': 'async',
        'type': 'south',
        'interface': '1.0',
        'config': config
    }


def test_plugin_init():
    assert coap.plugin_init(config) == config


@pytest.mark.asyncio
async def test_plugin_start(mocker, unused_port):
    # GIVEN
    port = {
        'description': 'Port to listen on',
        'type': 'integer',
        'default': str(unused_port()),
    }
    mocker.patch.dict(config, {'port': port})
    config['port']['value'] = config['port']['default']
    config['uri']['value'] = config['uri']['default']

    log_info = mocker.patch.object(coap._LOGGER, "info")
    assert coap.aiocoap_ctx is None

    try:
        # WHEN
        coap.plugin_start(config)
        await asyncio.sleep(.3)  # required to allow ensure_future task to complete

        # THEN
        assert coap.aiocoap_ctx is not None
        assert 1 == log_info.call_count
        calls = [call('CoAP listener started on port {} with uri {}'.format(config['port']['value'], config['uri']['value']))]
        log_info.assert_has_calls(calls, any_order=True)
    finally:
        cleanup_plugin()


@pytest.mark.asyncio
async def test_plugin_reconfigure(mocker, unused_port):
    # GIVEN
    port = {
        'description': 'Port to listen on',
        'type': 'integer',
        'default': str(unused_port()),
    }
    mocker.patch.dict(config, {'port': port})
    config['port']['value'] = config['port']['default']
    config['uri']['value'] = config['uri']['default']
    new_config = copy.deepcopy(_NEW_CONFIG)
    new_config['port']['value'] = new_config['port']['default']
    new_config['uri']['value'] = new_config['uri']['default']
    log_info = mocker.patch.object(coap._LOGGER, "info")

    try:
        # WHEN
        new_handle = coap.plugin_reconfigure(config, new_config)
        await asyncio.sleep(.3)  # required to allow ensure_future task to complete

        # THEN
        assert new_config == new_handle

        # TODO: assert plugin_shutdown, plugin_init, plugin_start called
        assert 3 == log_info.call_count
        calls = [call("Old config for CoAP plugin {} \n new config {}".format(config, new_config)),
                 call('Stopping South CoAP plugin...'),
                 call('CoAP listener started on port 1234 with uri sensor-values')]
        log_info.assert_has_calls(calls, any_order=True)
    finally:
        cleanup_plugin()


@pytest.mark.asyncio
async def test_plugin_shutdown(mocker, unused_port):
    # GIVEN
    port = {
        'description': 'Port to listen on',
        'type': 'integer',
        'default': str(unused_port()),
    }
    mocker.patch.dict(config, {'port': port})
    config['port']['value'] = config['port']['default']
    config['uri']['value'] = config['uri']['default']
    log_exception = mocker.patch.object(coap._LOGGER, "exception")
    log_info = mocker.patch.object(coap._LOGGER, "info")

    try:
        coap.plugin_start(config)
        await asyncio.sleep(.3)  # required to allow ensure_future task to complete

        # WHEN
        coap.plugin_shutdown(config)

        # THEN
        assert 2 == log_info.call_count  # includes start call log as well, as a GIVEN condition
        stop_call = [call('Stopping South CoAP plugin...')]
        log_info.assert_has_calls(stop_call, any_order=True)
        assert 0 == log_exception.call_count
    finally:
        cleanup_plugin()


class TestCoapSouthIngest(object):
    """Unit tests fledge.plugins.south.coap.coap.CoAPIngest
    """

    @pytest.mark.asyncio
    async def test_render_post_ok(self):
        data = """{
            "timestamp": "2017-01-02T01:02:03.23232Z-05:00",
            "asset": "sensor1",
            "key": "80a43623-ebe5-40d6-8d80-3f892da9b3b4",
            "readings": {
                "velocity": "500",
                "temperature": {
                    "value": "32",
                    "unit": "kelvin"
                }
            }
        }"""

        with patch.object(async_ingest, 'ingest_callback') as ingest_add_readings:
            request = message.Message(payload=cbor2.dumps(json.loads(data)), code=numbers.codes.Code.POST)
            r = await CoAPIngest.render_post(request)
            assert numbers.codes.Code.VALID == r.code
            assert '' == r.payload.decode()
            assert 1 == ingest_add_readings.call_count

    @pytest.mark.asyncio
    async def test_render_post_sensor_values_ok(self):
        data = """{
            "timestamp": "2017-01-02T01:02:03.23232Z-05:00",
            "asset": "sensor1",
            "key": "80a43623-ebe5-40d6-8d80-3f892da9b3b4",
            "sensor_values": {
                "velocity": "500",
                "temperature": {
                    "value": "32",
                    "unit": "kelvin"
                }
            }
        }"""
        with patch.object(async_ingest, 'ingest_callback') as ingest_add_readings:
            request = message.Message(payload=cbor2.dumps(json.loads(data)), code=numbers.codes.Code.POST)
            r = await CoAPIngest.render_post(request)
            assert numbers.codes.Code.VALID == r.code
            assert '' == r.payload.decode()
            assert 1 == ingest_add_readings.call_count

    @pytest.mark.asyncio
    async def test_render_post_reading_not_dict(self):
        data = """{
            "timestamp": "2017-01-02T01:02:03.23232Z-05:00",
            "asset": "sensor2",
            "key": "80a43623-ebe5-40d6-8d80-3f892da9b3b4",
            "readings": "500"
        }"""
        with patch.object(coap._LOGGER, "exception") as log_exception:
            with patch.object(async_ingest, 'ingest_callback') as ingest_add_readings:
                with pytest.raises(aiocoap.error.BadRequest) as excinfo:
                    request = message.Message(payload=cbor2.dumps(json.loads(data)), code=numbers.codes.Code.POST)
                    r = await CoAPIngest.render_post(request)
                    assert str(excinfo).endswith('readings must be a dictionary')
                assert 1 == log_exception.call_count
            assert 0 == ingest_add_readings.call_count
