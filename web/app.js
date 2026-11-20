(() => {
  const $ = (id) => document.getElementById(id);
  const uploadTab = $('upload-tab');
  const linkTab = $('link-tab');
  let sourceMode = 'upload';
  let activeJob = null;
  let elapsedTimer = null;
  let tipTimer = null;
  let currentJob = null;

  if (window.location.protocol === 'file:') {
    const warning = $('file-warning');
    warning.textContent = 'Preview only: to upload and generate clips, run “python web_app.py” in Terminal and open http://127.0.0.1:8765.';
    warning.classList.remove('hidden');
  }

  function setMode(mode) {
    sourceMode = mode;
    const upload = mode === 'upload';
    uploadTab.classList.toggle('active', upload);
    linkTab.classList.toggle('active', !upload);
    uploadTab.setAttribute('aria-selected', String(upload));
    linkTab.setAttribute('aria-selected', String(!upload));
    $('upload-panel').classList.toggle('hidden', !upload);
    $('link-panel').classList.toggle('hidden', upload);
  }
  uploadTab.addEventListener('click', () => setMode('upload'));
  linkTab.addEventListener('click', () => setMode('link'));

  const fileInput = $('video-file');
  fileInput.addEventListener('change', () => {
    const file = fileInput.files[0];
    $('file-label').textContent = file ? file.name : 'Choose an MP4 video';
  });
  const dropzone = $('dropzone');
  for (const event of ['dragenter', 'dragover']) {
    dropzone.addEventListener(event, (e) => { e.preventDefault(); dropzone.classList.add('drag'); });
  }
  for (const event of ['dragleave', 'drop']) {
    dropzone.addEventListener(event, (e) => { e.preventDefault(); dropzone.classList.remove('drag'); });
  }
  dropzone.addEventListener('drop', (e) => {
    const file = e.dataTransfer.files[0];
    if (!file) return;
    if (!file.name.toLowerCase().endsWith('.mp4')) return showError('Please choose an MP4 video.');
    const transfer = new DataTransfer();
    transfer.items.add(file);
    fileInput.files = transfer.files;
    $('file-label').textContent = file.name;
  });

  function options() {
    return {
      count: Number($('count').value),
      length: Number($('length').value),
      min_length: 15,
      reframe: $('reframe').value,
      visual_scan: $('visual-scan').checked
    };
  }
  function queryOptions() {
    return new URLSearchParams(options()).toString();
  }
  function showError(message) {
    const box = $('job');
    box.classList.remove('hidden');
    $('spinner').className = 'spinner failed';
    $('job-status').textContent = 'CHECK THIS';
    $('job-stage').textContent = 'Could not start';
    $('job-message').textContent = message;
  }
  function startJob(id) {
    activeJob = id;
    try { window.localStorage.setItem('stream-to-shorts-active-job', id); } catch {}
    $('job').classList.remove('hidden');
    $('results').classList.add('hidden');
    $('spinner').className = 'spinner';
    $('job-status').textContent = 'WORKING';
    $('job-stage').textContent = 'Preparing your video…';
    $('job-message').textContent = 'Long recordings can take a while to analyze and render.';
    $('upload-progress').classList.add('hidden');
    $('log-panel').classList.remove('hidden');
    if (elapsedTimer) window.clearInterval(elapsedTimer);
    if (tipTimer) window.clearInterval(tipTimer);
    elapsedTimer = window.setInterval(updateElapsed, 1000);
    tipTimer = window.setInterval(updateTip, 9000);
    $('job').scrollIntoView({ behavior: 'smooth', block: 'start' });
    pollJob(id);
  }
  function updateElapsed() {
    if (!currentJob || !currentJob.created) return;
    const seconds = Math.max(0, Math.floor((Date.now() - currentJob.created) / 1000));
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const remainder = seconds % 60;
    $('elapsed').textContent = hours
      ? `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(remainder).padStart(2, '0')}`
      : `${String(minutes).padStart(2, '0')}:${String(remainder).padStart(2, '0')}`;
  }
  function updateTip() {
    if (!currentJob || currentJob.status !== 'running') return;
    const tips = {
      download: 'The source video is being saved locally before analysis. Large VODs can take time to download.',
      audio: 'Audio helps surface reactions and high energy moments. The scan works through the source in the background.',
      visual: 'The visual scan checks frames across the whole recording. Long streams can spend a while in this step.',
      transcript: 'Speech is transcribed around promising moments, which helps preserve context in each short.',
      render: 'The selected moments are being reframed and encoded as vertical MP4 shorts.',
    };
    $('processing-tip-text').textContent = tips[currentJob.stage_code] || 'Audio, speech, chat when available, and visual signals help rank the moments.';
  }
  function renderSteps(job) {
    const list = $('job-steps');
    const steps = job.steps || [];
    const signature = steps.map((step) => `${step.id}:${step.label}`).join('|');
    if (list.dataset.signature !== signature) {
      list.replaceChildren();
      for (const step of steps) {
        const item = document.createElement('li');
        item.dataset.step = step.id;
        item.textContent = step.label;
        list.append(item);
      }
      list.dataset.signature = signature;
    }
    const activeIndex = steps.findIndex((step) => step.id === job.stage_code);
    for (const [index, item] of [...list.children].entries()) {
      item.className = '';
      item.removeAttribute('aria-current');
      if (job.status === 'done' || index < activeIndex) item.classList.add('is-done');
      else if (job.status === 'failed' && index === activeIndex) item.classList.add('is-failed');
      else if ((activeIndex < 0 && index === 0) || index === activeIndex) {
        item.classList.add('is-current');
        item.setAttribute('aria-current', 'step');
      }
    }
  }
  async function pollJob(id) {
    if (activeJob !== id) return;
    try {
      const response = await fetch(`/api/jobs/${id}`, { cache: 'no-store' });
      const job = await response.json();
      if (!response.ok) throw new Error(job.error || 'Could not load job status.');
      currentJob = job;
      updateElapsed();
      updateTip();
      $('job-stage').textContent = job.stage || 'Working';
      renderSteps(job);
      $('job-log').textContent = (job.logs || []).join('\n');
      $('job-log').scrollTop = $('job-log').scrollHeight;
      if (job.status === 'done') {
        $('spinner').className = 'spinner done';
        $('job-status').textContent = 'COMPLETE';
        $('job-message').textContent = `${job.clips.length} shorts are ready to watch and download.`;
        $('processing-tip-text').textContent = 'Your shorts are ready. Play them here or download the MP4 files.';
        renderClips(id, job.clips);
        return;
      }
      if (job.status === 'failed') {
        $('spinner').className = 'spinner failed';
        $('job-status').textContent = 'NEEDS ATTENTION';
        $('job-message').textContent = job.error || 'The job failed. Check the processing details below.';
        $('processing-tip-text').textContent = 'The processing log below may show what went wrong. Your uploaded source is still in the jobs folder.';
        return;
      }
      window.setTimeout(() => pollJob(id), 2000);
    } catch (error) {
      $('job-message').textContent = error.message;
      window.setTimeout(() => pollJob(id), 4000);
    }
  }
  function formatTime(seconds) {
    const total = Math.max(0, Math.floor(Number(seconds) || 0));
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
  }
  function renderClips(id, clips) {
    const grid = $('clip-grid');
    grid.replaceChildren();
    $('result-count').textContent = `${clips.length} clips`;
    for (let index = 0; index < clips.length; index++) {
      const clip = clips[index];
      const filename = String(clip.file || '').split(/[\\/]/).pop();
      const media = `/media/${id}/${encodeURIComponent(filename)}`;
      const card = document.createElement('article');
      card.className = 'clip-card';
      const player = document.createElement('video');
      player.src = media;
      player.controls = true;
      player.preload = 'metadata';
      player.playsInline = true;
      const info = document.createElement('div');
      info.className = 'clip-info';
      const title = document.createElement('div');
      title.className = 'clip-title';
      const number = document.createElement('span');
      number.textContent = `Short ${String(index + 1).padStart(2, '0')}`;
      const score = document.createElement('span');
      score.textContent = clip.rank == null ? '' : `#${clip.rank}`;
      title.append(number, score);
      const time = document.createElement('div');
      time.className = 'clip-time';
      time.textContent = `${formatTime(clip.start)} – ${formatTime(clip.end)}`;
      info.append(title, time);
      if (clip.signals) {
        const signals = document.createElement('div');
        signals.className = 'clip-signals';
        signals.textContent = `${clip.signals} signal${clip.signals === 1 ? '' : 's'}`;
        info.append(signals);
      }
      const link = document.createElement('a');
      link.className = 'download-link';
      link.href = media;
      link.download = filename;
      link.textContent = 'Download MP4';
      info.append(link);
      card.append(player, info);
      grid.append(card);
    }
    $('results').classList.remove('hidden');
    $('results').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function upload(file) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', `/api/jobs/upload?${queryOptions()}`);
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      xhr.setRequestHeader('X-Filename', encodeURIComponent(file.name));
      $('upload-progress').classList.remove('hidden');
      $('job-stage').textContent = 'Uploading your video…';
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) {
          const percent = Math.round(event.loaded / event.total * 100);
          $('upload-progress-bar').style.width = `${percent}%`;
          $('job-message').textContent = `Uploading ${file.name} · ${percent}%`;
        }
      };
      xhr.onload = () => {
        let body;
        try { body = JSON.parse(xhr.responseText); } catch { return reject(new Error('The server returned an invalid response.')); }
        if (xhr.status < 200 || xhr.status >= 300) return reject(new Error(body.error || 'Upload failed.'));
        resolve(body);
      };
      xhr.onerror = () => reject(new Error('Upload connection failed.'));
      xhr.send(file);
    });
  }

  $('generate').addEventListener('click', async () => {
    if (window.location.protocol === 'file:') {
      showError('This page was opened as a file, so it cannot reach the clipper. In Terminal, run `python web_app.py` from the project folder, then open http://127.0.0.1:8765.');
      return;
    }
    const button = $('generate');
    button.disabled = true;
    try {
      let result;
      if (sourceMode === 'upload') {
        const file = fileInput.files[0];
        if (!file) throw new Error('Choose an MP4 video first.');
        if (!file.name.toLowerCase().endsWith('.mp4')) throw new Error('Please choose an MP4 video.');
        $('job').classList.remove('hidden');
        $('spinner').className = 'spinner';
        $('job-status').textContent = 'UPLOADING';
        result = await upload(file);
      } else {
        const url = $('video-url').value.trim();
        if (!url) throw new Error('Paste a video link first.');
        $('job').classList.remove('hidden');
        $('spinner').className = 'spinner';
        $('job-status').textContent = 'STARTING';
        $('job-stage').textContent = 'Preparing download…';
        const response = await fetch('/api/jobs/link', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ...options(), url })
        });
        result = await response.json();
        if (!response.ok) throw new Error(result.error || 'Could not start processing.');
      }
      startJob(result.id);
    } catch (error) {
      showError(error.message);
    } finally {
      button.disabled = false;
    }
  });

  for (const id of ['count', 'length', 'reframe']) {
    $(id).addEventListener('change', () => {
      const framing = $('reframe').selectedOptions[0].textContent.replace(' full video', '');
      document.querySelector('.settings-summary').textContent = `${$('count').value || 0} shorts · ${$('length').value} sec · ${framing}`;
    });
  }

  try {
    const savedJob = window.localStorage.getItem('stream-to-shorts-active-job');
    if (savedJob) startJob(savedJob);
  } catch {}
})();
