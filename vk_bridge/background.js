'use strict';

var VK_RU_MAIN = 'https://vk.ru/';
var VK_RU_LOGIN = 'https://login.vk.ru/';
var VK_COM_MAIN = 'https://vk.com/';
var VK_COM_LOGIN = 'https://login.vk.com/';

function cookiesApiAvailable() {
  return (
    typeof chrome !== 'undefined' &&
    !!chrome.cookies &&
    typeof chrome.cookies.getAll === 'function'
  );
}

function getCookies(url) {
  return new Promise(function (resolve, reject) {
    if (!cookiesApiAvailable()) {
      reject(new Error('cookies-unavailable'));
      return;
    }

    var settled = false;

    function finish(cookies) {
      if (settled) {
        return;
      }
      settled = true;
      resolve(Array.isArray(cookies) ? cookies : []);
    }

    function fail() {
      if (settled) {
        return;
      }
      settled = true;
      reject(new Error('cookies-unavailable'));
    }

    try {
      var maybePromise = chrome.cookies.getAll({ url: url }, function (cookies) {
        if (chrome.runtime.lastError || !cookies) {
          fail();
          return;
        }
        finish(cookies);
      });

      if (maybePromise && typeof maybePromise.then === 'function') {
        maybePromise.then(finish).catch(fail);
      }
    } catch (_) {
      fail();
    }
  });
}

function pick(cookies, name) {
  if (!Array.isArray(cookies)) {
    return '';
  }
  for (var i = 0; i < cookies.length; i += 1) {
    var cookie = cookies[i];
    if (cookie && cookie.name === name && typeof cookie.value === 'string') {
      return cookie.value;
    }
  }
  return '';
}

function readCookieSets() {
  return Promise.all([
    getCookies(VK_RU_MAIN),
    getCookies(VK_RU_LOGIN),
    getCookies(VK_COM_MAIN),
    getCookies(VK_COM_LOGIN)
  ]).then(function (sets) {
    return {
      vkRuMain: sets[0],
      vkRuLogin: sets[1],
      vkComMain: sets[2],
      vkComLogin: sets[3]
    };
  });
}

function buildResponse(sets) {
  var remixsid = pick(sets.vkRuMain, 'remixsid') || pick(sets.vkRuLogin, 'remixsid');
  var p = pick(sets.vkRuLogin, 'p') || pick(sets.vkRuMain, 'p');
  var remixsidVkCom = pick(sets.vkComMain, 'remixsid') || pick(sets.vkComLogin, 'remixsid');
  var pVkCom = pick(sets.vkComLogin, 'p') || pick(sets.vkComMain, 'p');

  var domain = '';
  if (remixsid) {
    domain = 'vk.ru';
  } else if (remixsidVkCom || pVkCom) {
    domain = 'vk.com';
  }

  return {
    remixsid: remixsid,
    p: p,
    domain: domain,
    remixsidVkCom: remixsidVkCom,
    pVkCom: pVkCom
  };
}

chrome.runtime.onMessage.addListener(function (message, sender, sendResponse) {
  if (!message || message.type !== 'vk-cookies') {
    return false;
  }

  if (!cookiesApiAvailable()) {
    sendResponse({ error: 'cookies-unavailable' });
    return false;
  }

  readCookieSets()
    .then(function (sets) {
      sendResponse(buildResponse(sets));
    })
    .catch(function () {
      sendResponse({ error: 'cookies-unavailable' });
    });

  return true;
});
