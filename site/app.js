'use strict';

const $ = (id) => document.getElementById(id);

const scenes = {
  training: {
    title: '培训现场',
    index: '01',
    before: 'media/training-before.jpg',
    after: 'media/training-after.jpg',
    note: '生成的培训现场测试素材，经本地调色并合入右下角水印；拖动查看同一图像的处理前后。'
  },
  'training-detail': {
    title: '同图局部',
    index: '02',
    before: 'media/training-detail-before.jpg',
    after: 'media/training-detail-after.jpg',
    note: '同一张培训测试图的局部裁切，便于检查提亮、层次与右下角水印；不是另一张独立样本。'
  },
  portrait: {
    title: '人像细节',
    index: '03',
    before: 'media/portrait-before.jpg',
    after: 'media/portrait-after.jpg',
    skin: 'media/skin-overlay.jpg',
    note: '人像展示采用85%磨皮强度，已合入右下角水印；皮肤候选独立展示，实际活动仍按样片确认强度。'
  }
};

let scene = 'training';
let dialogScene = 'training';
let dialogVariant = 'after';
let returnFocus = null;
let selectionVersion = 0;
let downloadPackages = {};
let draggingCompare = false;

function setSplit(value) {
  const next = Math.max(0, Math.min(100, Number(value)));
  $('compare-range').value = String(next);
  $('compare-frame').style.setProperty('--split', next + '%');
  $('compare-range').setAttribute(
    'aria-valuetext',
    `原片显示 ${Math.round(next)}%，试修效果显示 ${Math.round(100 - next)}%`
  );
}

function splitFromPointer(clientX) {
  const rect = $('compare-frame').getBoundingClientRect();
  if (!rect.width) return Number($('compare-range').value);
  return ((clientX - rect.left) / rect.width) * 100;
}

$('compare-range').addEventListener('input', () => setSplit($('compare-range').value));
setSplit($('compare-range').value);

const frame = $('compare-frame');
frame.addEventListener('pointerdown', (event) => {
  if (event.target.closest('button')) return;
  draggingCompare = true;
  frame.setPointerCapture?.(event.pointerId);
  setSplit(splitFromPointer(event.clientX));
});
frame.addEventListener('pointermove', (event) => {
  if (!draggingCompare) return;
  setSplit(splitFromPointer(event.clientX));
});
const endDrag = (event) => {
  if (!draggingCompare) return;
  draggingCompare = false;
  try { frame.releasePointerCapture?.(event.pointerId); } catch (_) {}
};
frame.addEventListener('pointerup', endDrag);
frame.addEventListener('pointercancel', endDrag);
frame.addEventListener('dblclick', () => setSplit(50));

function preload(src) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(src);
    image.onerror = reject;
    image.src = src;
  });
}

async function chooseScene(key) {
  if (!scenes[key]) return;
  const version = ++selectionVersion;
  const data = scenes[key];
  $('asset-error').hidden = true;
  frame.classList.add('is-busy');
  frame.setAttribute('aria-busy', 'true');
  try {
    await Promise.all([preload(data.before), preload(data.after)]);
    if (version !== selectionVersion) return;
    scene = key;
    frame.classList.add('is-switching');
    $('hero-before').src = data.before;
    $('hero-after').src = data.after;
    $('hero-before').alt = data.title + '原片';
    $('hero-after').alt = data.title + '试修效果';
    $('scene-label').textContent = data.index + ' / ' + data.title;
    $('scene-note').textContent = data.note;
    $('compare-caption').textContent = key === 'portrait' ? '原片 × 修肤与水印' : '测试原图 × 调色与水印';
    $('tag-before').textContent = key === 'portrait' ? '原片' : '测试原图';
    document.querySelectorAll('[data-scene]').forEach((button) => {
      const active = button.dataset.scene === key;
      button.setAttribute('aria-pressed', String(active));
      button.setAttribute('aria-selected', String(active));
      button.classList.toggle('is-active', active);
    });
    setSplit(52);
    window.setTimeout(() => frame.classList.remove('is-switching'), 360);
  } catch (_) {
    if (version === selectionVersion) $('asset-error').hidden = false;
  } finally {
    if (version === selectionVersion) {
      frame.classList.remove('is-busy');
      frame.removeAttribute('aria-busy');
    }
  }
}

document.querySelectorAll('[data-scene]').forEach((button) => {
  button.addEventListener('click', () => chooseScene(button.dataset.scene));
});

function renderLightbox() {
  const data = scenes[dialogScene];
  if (!data[dialogVariant]) dialogVariant = 'after';
  $('lightbox-title').textContent =
    data.title + (dialogScene === 'portrait' ? '' : '（生成测试素材）') + ' · 放大检查';
  const variantText = { before: '原片', after: '试修＋水印', skin: '自动皮肤候选' };
  $('lightbox-image').src = data[dialogVariant];
  $('lightbox-image').alt = data.title + ' · ' + variantText[dialogVariant] + '，适应当前窗口';
  document.querySelectorAll('[data-modal-variant]').forEach((button) => {
    button.hidden = button.dataset.modalVariant === 'skin' && !data.skin;
    button.setAttribute('aria-pressed', String(button.dataset.modalVariant === dialogVariant));
  });
}

function openLightbox(key, variant, trigger) {
  dialogScene = key;
  dialogVariant = variant;
  returnFocus = trigger;
  renderLightbox();
  $('lightbox').showModal();
  document.body.classList.add('dialog-open');
  $('close-lightbox').focus();
}

$('hero-inspect').addEventListener('click', (event) => openLightbox(scene, 'after', event.currentTarget));
document.querySelectorAll('[data-detail]').forEach((button) => {
  button.addEventListener('click', () => openLightbox(button.dataset.detail, button.dataset.variant, button));
});
document.querySelectorAll('[data-modal-variant]').forEach((button) => {
  button.addEventListener('click', () => {
    dialogVariant = button.dataset.modalVariant;
    renderLightbox();
  });
});
$('close-lightbox').addEventListener('click', () => $('lightbox').close());
$('lightbox').addEventListener('click', (event) => {
  if (event.target === $('lightbox')) {
    const rect = $('lightbox').getBoundingClientRect();
    if (
      event.clientX < rect.left ||
      event.clientX > rect.right ||
      event.clientY < rect.top ||
      event.clientY > rect.bottom
    ) {
      $('lightbox').close();
    }
  }
});
$('lightbox').addEventListener('close', () => {
  document.body.classList.remove('dialog-open');
  if (returnFocus?.isConnected) returnFocus.focus();
});

async function copyText(text, button, feedback) {
  feedback.classList.remove('is-error');
  if (!text) {
    feedback.textContent = '下载信息尚未读取完成，请稍后重试。';
    feedback.classList.add('is-error');
    return;
  }
  try {
    if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
    await navigator.clipboard.writeText(text);
    feedback.textContent = '已复制，可以粘贴使用。';
    button.setAttribute('data-copied', 'true');
  } catch (_) {
    feedback.textContent = '未能自动复制，请选中上面的文字手动复制。';
    feedback.classList.add('is-error');
  }
}

$('copy-prompt').addEventListener('click', (event) =>
  copyText($('starter-prompt').textContent, event.currentTarget, $('prompt-feedback'))
);
document.querySelectorAll('[data-copy-sha]').forEach((button) => {
  button.addEventListener('click', () =>
    copyText(
      downloadPackages[button.dataset.copySha]?.sha256,
      button,
      button.parentElement.querySelector('.copy-feedback')
    )
  );
});

function validatePackage(pkg) {
  if (
    !pkg ||
    !['standard', 'offline'].includes(pkg.id) ||
    !Number.isSafeInteger(pkg.bytes) ||
    pkg.bytes <= 0 ||
    !/^[a-f0-9]{64}$/i.test(pkg.sha256) ||
    !/^[a-zA-Z0-9][a-zA-Z0-9._-]*\.zip$/.test(pkg.filename)
  ) {
    throw new Error('Invalid package record');
  }
  const url = new URL(pkg.url, location.href);
  if (
    url.origin !== location.origin ||
    !url.pathname.startsWith('/downloads/') ||
    url.pathname.split('/').at(-1) !== pkg.filename ||
    url.search ||
    url.hash
  ) {
    throw new Error('Invalid package URL');
  }
  return { ...pkg, url: url.href };
}

async function loadDownloads() {
  const status = $('download-status');
  status.classList.remove('is-error');
  status.textContent = '正在读取版本与下载信息……';
  try {
    const response = await fetch('downloads.json', { cache: 'no-cache' });
    if (!response.ok) throw new Error('Download manifest unavailable');
    const config = await response.json();
    if (!Array.isArray(config.packages) || !/^\d+\.\d+\.\d+$/.test(config.version)) {
      throw new Error('Invalid release');
    }
    const parsed = config.packages.map(validatePackage);
    if (parsed.length !== 2 || new Set(parsed.map((item) => item.id)).size !== 2) {
      throw new Error('Incomplete release');
    }
    downloadPackages = Object.fromEntries(parsed.map((item) => [item.id, item]));
    document.querySelectorAll('.version-text').forEach((node) => {
      node.textContent = config.version;
    });
    for (const pkg of parsed) {
      const link = document.querySelector(`[data-package="${pkg.id}"]`);
      link.href = pkg.url;
      link.download = pkg.filename;
      link.removeAttribute('aria-disabled');
      link.replaceChildren(document.createTextNode('下载' + (pkg.id === 'standard' ? '普通包' : '离线包') + ' '));
      const arrow = document.createElement('span');
      arrow.setAttribute('aria-hidden', 'true');
      arrow.textContent = '↓';
      link.append(arrow);
      document.querySelector(`[data-size="${pkg.id}"]`).textContent = (pkg.bytes / 1e6).toFixed(1) + ' MB';
      document.querySelector(`[data-sha="${pkg.id}"]`).textContent = pkg.sha256;
    }
    if (Number.isSafeInteger(config.verification?.regression_tests)) {
      document.querySelectorAll('[data-regression-count]').forEach((node) => {
        node.textContent = config.verification.regression_tests;
      });
    }
    if (Number.isSafeInteger(config.verification?.contract_checks)) {
      document.querySelectorAll('[data-contract-count]').forEach((node) => {
        node.textContent = config.verification.contract_checks;
      });
    }
    status.textContent = '版本 ' + config.version + ' · 文件大小与 SHA-256 校验值已列出';
  } catch (_) {
    status.classList.add('is-error');
    status.textContent = '下载信息暂时无法读取，请稍后重试。';
    const retry = document.createElement('button');
    retry.type = 'button';
    retry.textContent = '重新读取';
    retry.addEventListener('click', loadDownloads);
    status.append(retry);
  }
}

loadDownloads();

function setupHeaderAndProgress() {
  const header = $('site-header');
  const bar = $('scroll-progress-bar');
  const onScroll = () => {
    const max = document.documentElement.scrollHeight - window.innerHeight;
    const ratio = max > 0 ? window.scrollY / max : 0;
    if (bar) bar.style.width = Math.max(0, Math.min(1, ratio)) * 100 + '%';
    header?.classList.toggle('is-scrolled', window.scrollY > 8);
  };
  onScroll();
  window.addEventListener('scroll', onScroll, { passive: true });
}

function setupReveal() {
  const nodes = document.querySelectorAll('.reveal');
  if (!nodes.length) return;
  if (!('IntersectionObserver' in window) || window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    nodes.forEach((node) => node.classList.add('is-in'));
    return;
  }
  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        const delay = Number(entry.target.getAttribute('data-reveal-delay') || 0);
        window.setTimeout(() => entry.target.classList.add('is-in'), delay);
        observer.unobserve(entry.target);
      });
    },
    { threshold: 0.12, rootMargin: '0px 0px -8% 0px' }
  );
  nodes.forEach((node) => observer.observe(node));
}

function setupMotion() {
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)');
  if (reduce.matches || !window.gsap || !window.ScrollTrigger) return;
  gsap.registerPlugin(ScrollTrigger);
  const context = gsap.context(() => {
    gsap
      .timeline({
        scrollTrigger: {
          trigger: '.workflow-layout',
          start: 'top 65%',
          end: 'bottom 45%',
          scrub: 0.35
        }
      })
      .to('.process-progress', { scaleY: 1, ease: 'none', duration: 1 }, 0)
      .to('.workflow-photo-after', { clipPath: 'inset(0 0% 0 0)', ease: 'none', duration: 1 }, 0);

    if (document.querySelector('.workbench-frame img')) {
      gsap.fromTo(
        '.workbench-frame img',
        { yPercent: 0 },
        {
          yPercent: -8,
          ease: 'none',
          scrollTrigger: {
            trigger: '.workbench-frame',
            start: 'top bottom',
            end: 'bottom top',
            scrub: true
          }
        }
      );
    }
  });
  reduce.addEventListener(
    'change',
    (event) => {
      if (event.matches) context.revert();
    },
    { once: true }
  );
}

setupHeaderAndProgress();
setupReveal();
setupMotion();
