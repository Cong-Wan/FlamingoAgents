/*
Author: wilbur
Version: 1.2
Date: 2026-08-14
Description: 「技能」展示页（与模型配置平级）：打开时 GET /api/skills 一次渲染卡片列表。
             v1.1：render 加 renderSeq 序号守卫，防快速进出时晚到的响应叠加旧卡片。
             v1.2：卡片加「编辑」；弹层按模板结构化编辑（Esc 栈接入）；保存后刷新列表并 reloadSkills。
*/
(function () {
  'use strict';

  var listEl = document.getElementById('skillsList');
  var modalEl = document.getElementById('skillEditModal');
  var nameInput = document.getElementById('skillEditName');
  var descInput = document.getElementById('skillEditDescription');
  var enabledInput = document.getElementById('skillEditEnabled');
  var bodyInput = document.getElementById('skillEditBody');
  var errorEl = document.getElementById('skillEditError');
  var cancelBtn = document.getElementById('skillEditCancel');
  var saveBtn = document.getElementById('skillEditSave');
  var renderSeq = 0; // 防快速进出时晚到的响应叠加旧卡片
  var originalName = '';
  var saving = false;

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
        var editBtn = document.createElement('button');
        editBtn.className = 'btn skill-card-edit-btn';
        editBtn.type = 'button';
        editBtn.textContent = '编辑';
        editBtn.addEventListener('click', function () { openEdit(skill.name); });
        var badge = document.createElement('span');
        badge.className = 'skill-card-badge' + (skill.disabled ? ' skill-card-badge-off' : '');
        badge.textContent = skill.disabled ? '不进 prompt' : '已启用';
        head.appendChild(nameEl);
        head.appendChild(editBtn);
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

  function hideEditError() {
    errorEl.textContent = '';
    errorEl.classList.add('hidden');
  }

  function showEditError(message) {
    errorEl.textContent = message || '保存失败';
    errorEl.classList.remove('hidden');
  }

  function closeSkillEditModal() {
    modalEl.classList.add('hidden');
    window.appStore.removeModalClose(closeSkillEditModal);
  }

  function fillForm(data) {
    nameInput.value = data.name || '';
    descInput.value = data.description || '';
    enabledInput.checked = !data.disabled;
    bodyInput.value = data.body || '';
    hideEditError();
  }

  function openEdit(name) {
    window.api.getSkillBody(name).then(function (data) {
      originalName = (data && data.name) || name;
      fillForm(data || {});
      modalEl.classList.remove('hidden');
      window.appStore.pushModalClose(closeSkillEditModal);
    }).catch(function (error) {
      if (error && error.status === 404) {
        if (window.toast) window.toast('技能已不存在，正在刷新列表');
        render();
        return;
      }
      if (window.toast) window.toast('加载技能失败：' + ((error && error.message) || '未知错误'));
    });
  }

  function saveEdit() {
    if (saving || !originalName) return;
    saving = true;
    saveBtn.disabled = true;
    hideEditError();
    window.api.saveSkill(originalName, {
      name: nameInput.value.trim(),
      description: descInput.value,
      disabled: !enabledInput.checked,
      body: bodyInput.value
    }).then(function () {
      closeSkillEditModal();
      render();
      if (window.slashCommand && window.slashCommand.reloadSkills) {
        window.slashCommand.reloadSkills();
      }
    }).catch(function (error) {
      showEditError((error && error.message) || '保存失败');
    }).then(function () {
      saving = false;
      saveBtn.disabled = false;
    });
  }

  cancelBtn.addEventListener('click', closeSkillEditModal);
  saveBtn.addEventListener('click', saveEdit);

  window.skillsView = {
    open: function () { render(); }
  };
})();
