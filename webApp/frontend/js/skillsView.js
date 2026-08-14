/*
Author: wilbur
Version: 1.1
Date: 2026-08-14
Description: 「技能」只读展示页（与模型配置平级）：打开时 GET /api/skills 一次渲染卡片列表；
             技能本体在磁盘 config/skills/ 由 agent 只读加载，本页只读不编辑。
             v1.1：render 加 renderSeq 序号守卫，防快速进出时晚到的响应叠加旧卡片。
*/
(function () {
  'use strict';

  var listEl = document.getElementById('skillsList');
  var renderSeq = 0; // 防快速进出时晚到的响应叠加旧卡片

  function render() {
    var seq = ++renderSeq;
    listEl.innerHTML = '';
    window.api.getSkills().then(function (data) {
      if (seq !== renderSeq) return;
      var skills = (data && data.skills) || [];
      if (skills.length === 0) {
        var empty = document.createElement('div');
        empty.className = 'skills-empty';
        empty.textContent = 'config/skills/ 下暂无技能';
        listEl.appendChild(empty);
        return;
      }
      skills.forEach(function (skill) {
        var card = document.createElement('div');
        card.className = 'skill-card';
        var head = document.createElement('div');
        head.className = 'skill-card-head';
        var nameEl = document.createElement('span');
        nameEl.className = 'skill-card-name';
        nameEl.textContent = skill.name || '';
        var badge = document.createElement('span');
        badge.className = 'skill-card-badge' + (skill.disabled ? ' skill-card-badge-off' : '');
        badge.textContent = skill.disabled ? '不进 prompt' : '已启用';
        head.appendChild(nameEl);
        head.appendChild(badge);
        card.appendChild(head);
        var desc = document.createElement('div');
        desc.className = 'skill-card-desc';
        desc.textContent = skill.description || '';
        card.appendChild(desc);
        var pathEl = document.createElement('div');
        pathEl.className = 'skill-card-path';
        pathEl.textContent = skill.filePath || '';
        card.appendChild(pathEl);
        listEl.appendChild(card);
      });
    }).catch(function () {
      if (seq !== renderSeq) return;
      var failed = document.createElement('div');
      failed.className = 'skills-empty';
      failed.textContent = '技能列表加载失败';
      listEl.appendChild(failed);
    });
  }

  window.skillsView = {
    open: function () { render(); }
  };
})();
