'use strict';

(function () {
  var REQUEST_SOURCE = 'pulsar-vk-request';
  var BRIDGE_SOURCE = 'pulsar-vk-bridge';

  function markBridge() {
    if (document.documentElement) {
      document.documentElement.dataset.pulsarVkBridge = '1';
    }
  }

  markBridge();
  if (!document.documentElement) {
    document.addEventListener('DOMContentLoaded', markBridge, { once: true });
  }

  function reply(id, body) {
    try {
      var message = { source: BRIDGE_SOURCE, id: id };
      if (body.error) {
        message.error = body.error;
      } else {
        message.payload = body.payload;
      }
      window.postMessage(message, location.origin);
    } catch (_) {
      // Never log or expose cookie data.
    }
  }

  function replyError(id, code) {
    reply(id, { error: code });
  }

  function asString(value) {
    return typeof value === 'string' ? value : '';
  }

  window.addEventListener('message', function (event) {
    if (event.source !== window || event.origin !== location.origin) {
      return;
    }

    var request = event.data;
    if (!request || typeof request !== 'object') {
      return;
    }
    if (request.source !== REQUEST_SOURCE || request.type !== 'cookies') {
      return;
    }
    if (typeof request.id !== 'string' || request.id.length === 0) {
      return;
    }

    var id = request.id;
    var userAgent = typeof request.userAgent === 'string' && request.userAgent
      ? request.userAgent
      : navigator.userAgent;

    try {
      if (
        typeof chrome === 'undefined' ||
        !chrome.runtime ||
        typeof chrome.runtime.sendMessage !== 'function'
      ) {
        replyError(id, 'bridge-unavailable');
        return;
      }

      chrome.runtime.sendMessage({ type: 'vk-cookies' }, function (response) {
        if (chrome.runtime.lastError || !response) {
          replyError(id, 'bridge-unavailable');
          return;
        }
        if (response.error) {
          replyError(id, typeof response.error === 'string' ? response.error : 'bridge-unavailable');
          return;
        }

        reply(id, {
          payload: {
            remixsid: asString(response.remixsid),
            p: asString(response.p),
            domain: asString(response.domain),
            remixsidVkCom: asString(response.remixsidVkCom),
            pVkCom: asString(response.pVkCom),
            userAgent: userAgent
          }
        });
      });
    } catch (_) {
      replyError(id, 'bridge-unavailable');
    }
  });
})();
