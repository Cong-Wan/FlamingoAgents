/*
Author: wilbur
Version: 1.2
Date: 2026-09-08
Description: 聊天图片草稿：选择/粘贴/拖入 PNG JPEG WebP，缩略图预览，发送时输出 {name,mimeType,data} base64。
             按当前会话模型 input 是否含 image 启用入口；不支持时不消费图片粘贴。v1.2 加号悬停/点击弹出二级菜单，菜单项才是上传图片。
*/
(function () {
  'use strict';

  var MAX_COUNT = 4;
  var MAX_EACH = 5 * 1024 * 1024;
  var ALLOWED = { 'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp' };
  var drafts = [];
  var generation = 0;
  var modelsConfig = null;
  var thumbCache = {};
  var fileInput = null;
  var chipRow = null;
  var addButton = null;
  var addWrap = null;
  var addMenu = null;
  var uploadMenuItem = null;
  var menuPinned = false;
  var menuHoverTimer = null;
  var menuCloseFn = null;

  function toast(message) {
    if (window.toast) window.toast(message);
  }

  function currentSupportsImage() {
    var session = window.appStore && window.appStore.findSession(window.appStore.currentSessionId);
    if (!session || !modelsConfig || !modelsConfig.providers) return false;
    var provider = modelsConfig.providers[session.providerId];
    var models = provider && provider.models;
    if (!Array.isArray(models)) return false;
    for (var i = 0; i < models.length; i++) {
      if (models[i].id === session.modelId) {
        return Array.isArray(models[i].input) && models[i].input.indexOf('image') !== -1;
      }
    }
    return false;
  }

  function isImagePath(path) {
    return /\.(png|jpe?g|webp)$/i.test(path || '');
  }

  function fileToDraft(file) {
    return new Promise(function (resolve, reject) {
      if (!file || !ALLOWED[file.type]) {
        reject(new Error('仅支持 PNG / JPEG / WebP'));
        return;
      }
      if (file.size > MAX_EACH) {
        reject(new Error('单张图片不能超过 5MiB'));
        return;
      }
      var reader = new FileReader();
      reader.onload = function () {
        var result = reader.result;
        var comma = result.indexOf(',');
        var data = comma >= 0 ? result.slice(comma + 1) : result;
        resolve({
          id: 'd' + Date.now() + Math.random().toString(16).slice(2),
          name: file.name || 'image',
          mimeType: file.type,
          data: data,
          objectUrl: URL.createObjectURL(file)
        });
      };
      reader.onerror = function () { reject(new Error('读取图片失败')); };
      reader.readAsDataURL(file);
    });
  }

  function renderDrafts() {
    if (!chipRow) return;
    chipRow.innerHTML = '';
    if (drafts.length === 0) {
      chipRow.classList.add('hidden');
      return;
    }
    chipRow.classList.remove('hidden');
    drafts.forEach(function (draft) {
      var chip = document.createElement('span');
      chip.className = 'image-draft-chip';
      var img = document.createElement('img');
      img.className = 'image-draft-thumb';
      img.alt = draft.name;
      img.src = draft.objectUrl;
      var label = document.createElement('span');
      label.textContent = draft.name;
      label.title = draft.name;
      var remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'attachment-chip-remove';
      remove.textContent = '✕';
      remove.addEventListener('click', function (event) {
        event.preventDefault();
        removeDraft(draft.id);
      });
      chip.appendChild(img);
      chip.appendChild(label);
      chip.appendChild(remove);
      chipRow.appendChild(chip);
    });
  }

  function revokeDraft(draft) {
    if (draft && draft.objectUrl) URL.revokeObjectURL(draft.objectUrl);
  }

  function removeDraft(id) {
    drafts = drafts.filter(function (draft) {
      if (draft.id === id) {
        revokeDraft(draft);
        return false;
      }
      return true;
    });
    renderDrafts();
  }

  function addDrafts(newDrafts) {
    newDrafts.forEach(function (draft) {
      if (drafts.length >= MAX_COUNT) {
        toast('单条最多 4 张图片');
        revokeDraft(draft);
        return;
      }
      drafts.push(draft);
    });
    renderDrafts();
  }

  async function addFiles(fileList) {
    var files = Array.prototype.slice.call(fileList || []);
    var sessionId = window.appStore.currentSessionId;
    var gen = generation;
    for (var i = 0; i < files.length; i++) {
      try {
        var draft = await fileToDraft(files[i]);
        if (sessionId !== window.appStore.currentSessionId || gen !== generation) {
          revokeDraft(draft);
          return;
        }
        addDrafts([draft]);
      } catch (error) {
        toast(error.message || '无法添加图片');
      }
    }
  }

  function isMenuOpen() {
    return !!(addMenu && !addMenu.classList.contains('hidden'));
  }

  function closeMenu() {
    if (menuHoverTimer) {
      clearTimeout(menuHoverTimer);
      menuHoverTimer = null;
    }
    menuPinned = false;
    if (addMenu) addMenu.classList.add('hidden');
    if (addButton) addButton.setAttribute('aria-expanded', 'false');
    if (menuCloseFn && window.appStore && window.appStore.removeModalClose) {
      window.appStore.removeModalClose(menuCloseFn);
    }
    menuCloseFn = null;
  }

  function openMenu(pinned) {
    if (!addButton || addButton.disabled || !addMenu) return;
    if (menuHoverTimer) {
      clearTimeout(menuHoverTimer);
      menuHoverTimer = null;
    }
    if (pinned) menuPinned = true;
    addMenu.classList.remove('hidden');
    addButton.setAttribute('aria-expanded', 'true');
    if (pinned && window.appStore && window.appStore.pushModalClose && !menuCloseFn) {
      menuCloseFn = closeMenu;
      window.appStore.pushModalClose(menuCloseFn);
    }
  }

  function syncButton() {
    if (!addButton) return;
    var idle = !!window.appStore.currentSessionId && !window.appStore.stream;
    addButton.disabled = !idle;
    addButton.title = '添加';
    if (uploadMenuItem) {
      var canUpload = idle && currentSupportsImage();
      uploadMenuItem.disabled = !canUpload;
      uploadMenuItem.title = currentSupportsImage() ? '上传图片' : '当前模型未勾选 image，无法输入图片';
    }
    if (!idle) closeMenu();
  }

  async function refreshCapability() {
    try {
      modelsConfig = await window.api.getModels();
    } catch (error) {
      modelsConfig = modelsConfig || null;
    }
    syncButton();
  }

  function bindUi() {
    chipRow = document.getElementById('imageDrafts');
    addButton = document.getElementById('imageAddButton');
    fileInput = document.getElementById('imageFileInput');
    addWrap = document.getElementById('imageAddWrap');
    addMenu = document.getElementById('imageAddMenu');
    uploadMenuItem = document.getElementById('imageUploadMenuItem');
    if (addButton && fileInput) {
      addButton.addEventListener('click', function (event) {
        event.preventDefault();
        event.stopPropagation();
        if (addButton.disabled) return;
        if (isMenuOpen() && menuPinned) closeMenu();
        else openMenu(true);
      });
      fileInput.addEventListener('change', function () {
        addFiles(fileInput.files || []);
        fileInput.value = '';
        closeMenu();
      });
    }
    if (uploadMenuItem) {
      uploadMenuItem.addEventListener('click', function (event) {
        event.preventDefault();
        event.stopPropagation();
        if (uploadMenuItem.disabled || !fileInput) return;
        fileInput.click();
      });
    }
    if (addWrap) {
      addWrap.addEventListener('mouseenter', function () {
        if (!addButton || addButton.disabled) return;
        openMenu(false);
      });
      addWrap.addEventListener('mouseleave', function () {
        if (menuPinned) return;
        menuHoverTimer = setTimeout(function () {
          if (!menuPinned) closeMenu();
        }, 140);
      });
    }
    document.addEventListener('click', function (event) {
      if (!isMenuOpen() || !addWrap) return;
      if (addWrap.contains(event.target)) return;
      closeMenu();
    });
    var composer = document.getElementById('composerInput');
    if (composer) {
      composer.addEventListener('paste', function (event) {
        if (!currentSupportsImage() || addButton && addButton.disabled) return;
        var items = event.clipboardData && event.clipboardData.items;
        if (!items) return;
        var files = [];
        for (var i = 0; i < items.length; i++) {
          if (items[i].kind === 'file' && ALLOWED[items[i].type]) {
            files.push(items[i].getAsFile());
          }
        }
        if (files.length === 0) return;
        event.preventDefault();
        addFiles(files);
      });
      var wrap = document.querySelector('.composer');
      if (wrap) {
        wrap.addEventListener('dragover', function (event) {
          if (!currentSupportsImage()) return;
          event.preventDefault();
        });
        wrap.addEventListener('drop', function (event) {
          if (!currentSupportsImage() || !event.dataTransfer || !event.dataTransfer.files.length) return;
          event.preventDefault();
          addFiles(event.dataTransfer.files);
        });
      }
    }
  }

  function clearDrafts(revoke) {
    if (revoke !== false) drafts.forEach(revokeDraft);
    drafts = [];
    renderDrafts();
  }

  function snapshotDrafts() {
    return drafts.map(function (draft) {
      return { id: draft.id, name: draft.name, mimeType: draft.mimeType, data: draft.data, objectUrl: draft.objectUrl };
    });
  }

  function restoreDrafts(items) {
    clearDrafts();
    (items || []).forEach(function (item) {
      drafts.push(item);
    });
    renderDrafts();
  }

  function payload() {
    return drafts.map(function (draft) {
      return { name: draft.name, mimeType: draft.mimeType, data: draft.data };
    });
  }

  function displayImages() {
    return drafts.map(function (draft) {
      return { name: draft.name, objectUrl: draft.objectUrl, mimeType: draft.mimeType };
    });
  }

  async function loadThumb(sessionId, ref) {
    var key = sessionId + ':' + ref;
    if (thumbCache[key]) return thumbCache[key];
    var blob = await window.api.getSessionImage(sessionId, ref);
    var url = URL.createObjectURL(blob);
    thumbCache[key] = url;
    return url;
  }

  function resetForSession() {
    generation += 1;
    closeMenu();
    clearDrafts();
    Object.keys(thumbCache).forEach(function (key) {
      URL.revokeObjectURL(thumbCache[key]);
    });
    thumbCache = {};
    refreshCapability();
  }

  window.imageInput = {
    bindUi: bindUi,
    refreshCapability: refreshCapability,
    syncButton: syncButton,
    supportsImage: currentSupportsImage,
    isImagePath: isImagePath,
    hasDrafts: function () { return drafts.length > 0; },
    payload: payload,
    displayImages: displayImages,
    snapshotDrafts: snapshotDrafts,
    restoreDrafts: restoreDrafts,
    clearDrafts: clearDrafts,
    resetForSession: resetForSession,
    loadThumb: loadThumb
  };

  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', bindUi);
    } else {
      bindUi();
    }
  }
})();
