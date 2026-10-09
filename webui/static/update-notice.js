/* 旧版界面的 GitHub 更新提示。
 *
 * 只读取服务端缓存的检查结论（/api/update/status），不直接访问 GitHub，
 * 也不会触发新的检查；挂载点由模板提供：
 *   <a data-update-notice="flex" style="display:none">…<span data-update-notice-text></span></a>
 * data-update-notice 的值就是显示时使用的 display（侧栏用 flex，顶部导航用 inline-flex）。
 */
(function () {
  'use strict';

  var nodes = document.querySelectorAll('[data-update-notice]');
  if (!nodes.length || typeof fetch !== 'function') return;

  function label(state) {
    if (typeof state.behind === 'number' && state.behind > 0) {
      return '发现新版本（' + state.behind + ' 个新提交）';
    }
    return '发现新版本';
  }

  function tip(state) {
    var local = (state.local && state.local.short) || '-';
    var remote = (state.remote && state.remote.short) || '-';
    return '当前 ' + local + ' → 远端 ' + remote + '，点击查看更新详情';
  }

  fetch('/api/update/status', {
    headers: { Accept: 'application/json' },
    credentials: 'same-origin',
    cache: 'no-store'
  })
    .then(function (response) {
      return response.ok ? response.json() : null;
    })
    .then(function (state) {
      if (!state || !state.update_available) return;
      Array.prototype.forEach.call(nodes, function (node) {
        var text = node.querySelector('[data-update-notice-text]');
        if (text) {
          text.textContent = label(state);
        } else {
          node.textContent = label(state);
        }
        node.title = tip(state);
        node.style.display = node.getAttribute('data-update-notice') || 'inline-flex';
      });
    })
    .catch(function () {
      /* 提示失败不影响主界面；详细原因在系统配置页和日志里。 */
    });
})();
