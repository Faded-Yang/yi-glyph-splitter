/* api.js —— 极简 fetch 封装：统一 JSON、错误消息（detail 为中文说明） */
(function () {
  'use strict';
  async function req(method, url, body) {
    var opt = { method: method, headers: {}, cache: 'no-store' };
    if (body !== undefined) {
      opt.headers['Content-Type'] = 'application/json';
      opt.body = JSON.stringify(body);
    }
    var res;
    try {
      res = await fetch(url, opt);
    } catch (e) {
      throw new Error('网络错误：' + (e && e.message ? e.message : e));
    }
    var ct = res.headers.get('content-type') || '';
    var data = null;
    if (ct.indexOf('application/json') >= 0) {
      try { data = await res.json(); } catch (e2) { data = null; }
    }
    if (!res.ok) {
      var detail = data && (data.detail || data.message);
      if (detail && typeof detail !== 'string') { detail = JSON.stringify(detail); }
      var msg = detail || (res.status + ' ' + (res.statusText || '请求失败'));
      var err = new Error(msg);
      err.status = res.status;
      err.payload = data;
      throw err;
    }
    return data;
  }
  window.YI = window.YI || {};
  window.YI.api = {
    get: function (u) { return req('GET', u); },
    post: function (u, b) { return req('POST', u, b === undefined ? {} : b); },
    put: function (u, b) { return req('PUT', u, b); },
    del: function (u) { return req('DELETE', u); }
  };
})();