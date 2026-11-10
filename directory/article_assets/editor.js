(() => {
  const root = document.getElementById('article-editor');
  if (!root) return;
  const authorMode = root.dataset.authorMode === 'true';
  const baseUrl = root.dataset.baseUrl || '/admin/articles';
  const apiUrl = `${baseUrl}/api`;
  const form = document.getElementById('article-form');
  let state = JSON.parse(document.getElementById('article-initial-state').textContent);
  const message = document.getElementById('article-message');
  const status = document.getElementById('article-save-status');
  const publish = document.getElementById('article-publish');
  const save = document.getElementById('article-save');
  const withdraw = document.getElementById('article-withdraw');
  const submit = document.getElementById('article-submit');
  const cancelSubmission = document.getElementById('article-cancel-submission');
  const requestChanges = document.getElementById('article-request-changes');
  const reject = document.getElementById('article-reject');
  const editTab = document.getElementById('article-edit-tab');
  const previewTab = document.getElementById('article-preview-tab');
  const editPanel = document.getElementById('article-edit-panel');
  const previewPanel = document.getElementById('article-preview-panel');
  const snapshot = () => {
    const content = {
    title: form.elements.title.value,
    summary: form.elements.summary.value,
    author: form.elements.author.value,
    markdown: form.elements.markdown.value,
    tags: form.elements.tags.value.split(',').map(tag => tag.trim()).filter(Boolean),
    canonical_url: form.elements.canonical_url.value.trim() || null,
    cover_id: form.elements.cover_id.value || null,
    };
    if (form.elements.sponsored?.checked) {
      content.publication_type = 'sponsored';
      content.sponsor_name = form.elements.sponsor_name.value;
      content.sponsored_plan = form.elements.sponsored_plan.value;
    } else if (!form.elements.sponsored && state?.latest.snapshot.publication_type === 'sponsored') {
      for (const name of ['publication_type', 'sponsor_name', 'sponsored_plan']) content[name] = state.latest.snapshot[name];
    }
    return content;
  };
  let savedFingerprint = JSON.stringify(snapshot());
  let busy = false;
  let dirty = !state;
  const labels = {draft: 'Draft', published: 'Published', published_with_draft: 'Published with a new draft', withdrawn: 'Withdrawn',
    submitted: 'Pending review', changes_requested: 'Changes requested', rejected: 'Rejected', approved: 'Approved'};
  const errors = {version_conflict: 'A newer revision exists. Your changes were not overwritten. Keep your edits and reload the article.', slug_in_use: 'This slug is already in use.',
    invalid_slug: 'The slug is invalid or reserved.', invalid_markdown: 'The body is empty or exceeds 128 KiB.', invalid_image_url: 'The article contains an invalid image URL.',
    media_not_found: 'A referenced image was not found.', invalid_image: 'The image file is invalid.', invalid_image_type: 'This image format is not supported.',
    invalid_image_size: 'The image is empty or exceeds 2 MiB.', invalid_image_dimensions: 'The image dimensions are too large.',
    article_withdrawn: 'This article has been withdrawn and cannot be published again.', snapshot_corrupt: 'Version verification failed. Nothing was published.',
    article_not_submitted: 'The current version has not been submitted for review.', review_not_pending: 'The review status has changed. Please reload.',
    invalid_review_feedback: 'Please enter review feedback.',
    article_order_locked: 'This sponsored version is frozen.', article_payment_required: 'Verified payment is required before publication.',
    article_order_type_locked: 'A sponsored order cannot be changed into a free publication.',
    sponsored_link_limit: 'The article exceeds the external link allowance for its publication plan.',
    invalid_sponsor_name: 'Please enter the sponsor name.'};
  const orderLabels = {awaiting_confirmation: 'Awaiting author confirmation', awaiting_payment: 'Awaiting payment',
    payment_pending: 'Payment pending', creation_unknown: 'Invoice creation requires review', paid: 'Paid, publication pending',
    published: 'Published', cancelled: 'Cancelled', expired: 'Expired', manual_review: 'Payment requires review', refunded: 'Refunded'};
  const showMessage = (text, error = false) => {
    message.textContent = text;
    message.classList.toggle('error', error);
    message.hidden = false;
  };
  const refreshControls = () => {
    const archived = state && state.status === 'withdrawn';
    const sponsored = state?.latest.snapshot.publication_type === 'sponsored';
    const locked = state?.order && (!['cancelled', 'expired', 'refunded'].includes(state.order.status) || state.order.paid_at);
    save.disabled = busy || archived || locked;
    if (publish) {
      publish.disabled = busy || dirty || !state || archived || state.status === 'published'
        || (sponsored ? (state.order ? state.order.status !== 'paid' : state.review_status !== 'submitted')
          : state.submission && state.review_status !== 'submitted');
      document.getElementById('article-publish-label').textContent = sponsored
        ? (state?.order ? 'Publish approved article' : 'Approve and freeze') : state?.submission ? 'Approve and publish' : 'Publish';
    }
    if (withdraw) { withdraw.disabled = busy; withdraw.hidden = !state || !state.published || archived; }
    if (submit) submit.disabled = busy || archived || locked || (!dirty && state && ['submitted', 'published'].includes(state.review_status === 'submitted' ? 'submitted' : state.status));
    if (cancelSubmission) { cancelSubmission.disabled = busy || dirty; cancelSubmission.hidden = !state || archived || state.review_status !== 'submitted'; }
    for (const button of [requestChanges, reject]) if (button) button.disabled = busy || dirty || !state || archived || state.review_status !== 'submitted';
    const reviewPanel = document.getElementById('article-review-panel');
    reviewPanel.hidden = !state?.submission || (authorMode && !state.submission.feedback);
    const reviewText = document.getElementById('article-review-feedback-text');
    if (reviewText) reviewText.textContent = state?.submission?.feedback ? `V${state.submission.version}: ${state.submission.feedback}` : '';
    const reviewInput = document.getElementById('article-review-feedback');
    if (reviewInput) reviewInput.disabled = busy || archived || locked;
    for (const element of form.elements) element.disabled = busy || archived || locked;
    if (form.elements.sponsored) {
      form.elements.sponsored.disabled ||= Boolean(state?.order);
      form.elements.sponsor_name.required = form.elements.sponsored.checked;
      document.getElementById('article-sponsor-details').hidden = !form.elements.sponsored.checked;
    }
    document.getElementById('article-upload').disabled = busy || archived || locked;
    const orderPanel = document.getElementById('article-editor-order');
    orderPanel.hidden = !state?.order;
    if (state?.order) {
      document.getElementById('article-editor-order-status').textContent = orderLabels[state.order.status] || state.order.status;
      document.getElementById('article-editor-order-link').href = `${authorMode ? '/account' : '/admin'}/article-orders/${state.order.id}`;
    }
    previewTab.disabled = busy;
    const publicLink = document.getElementById('article-open-public');
    publicLink.hidden = !state || !state.published || archived;
    if (state) publicLink.href = `/articles/${state.slug}`;
    const displayStatus = ['submitted', 'changes_requested', 'rejected'].includes(state?.review_status) ? state.review_status : state?.status;
    status.textContent = dirty ? 'Unsaved' : labels[displayStatus] || 'Unsaved';
    document.getElementById('article-version').textContent = state ? `V${state.latest.version}` : '';
  };
  const markDirty = () => { dirty = !state || JSON.stringify(snapshot()) !== savedFingerprint; refreshControls(); };
  form.addEventListener('input', markDirty);
  form.addEventListener('change', markDirty);
  form.addEventListener('submit', event => event.preventDefault());
  window.addEventListener('beforeunload', event => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });
  const request = async (url, body, raw = false) => {
    const response = await fetch(url, {method: 'POST', credentials: 'same-origin', headers: {
      'Content-Type': raw ? 'application/octet-stream' : 'application/json'}, body: raw ? body : JSON.stringify(body)});
    let result;
    try { result = await response.json(); } catch { throw new Error('The server returned an invalid response. Your current content has been kept.'); }
    if (!response.ok) throw new Error(errors[result.detail] || (typeof result.detail === 'string' ? result.detail : 'The operation failed.'));
    return result;
  };
  const updateState = result => {
    state = result;
    const content = state.latest.snapshot;
    document.getElementById('article-editor-heading').textContent = state.status === 'withdrawn' ? 'Withdrawn article' : 'Edit article';
    document.title = `${content.title} · ${authorMode ? 'My articles' : 'Article management'}`;
    const publishedPanel = document.getElementById('article-published-version');
    publishedPanel.hidden = !state.published;
    if (state.published) {
      document.getElementById('article-published-summary').textContent = `${state.status === 'withdrawn' ? 'Last published version before withdrawal' : 'Current published version'} V${state.published.version}`;
      document.getElementById('article-published-title').textContent = state.published.snapshot.title;
      document.getElementById('article-published-markdown').textContent = state.published.snapshot.markdown;
    }
    for (const field of ['title', 'summary', 'author', 'markdown']) form.elements[field].value = content[field];
    form.elements.tags.value = content.tags.join(', ');
    form.elements.canonical_url.value = content.canonical_url || '';
    if (form.elements.sponsored) {
      form.elements.sponsored.checked = content.publication_type === 'sponsored';
      form.elements.sponsor_name.value = content.sponsor_name || '';
      form.elements.sponsored_plan.value = content.sponsored_plan || 'standard';
    }
    const coverSelect = form.elements.cover_id;
    if (content.cover_id && !Array.from(coverSelect.options).some(option => option.value === content.cover_id)) {
      const option = document.createElement('option'); option.value = content.cover_id; option.textContent = content.cover_id;
      coverSelect.append(option);
    }
    coverSelect.value = content.cover_id || '';
    form.elements.slug.value = state.slug;
    form.elements.slug.readOnly = true;
    savedFingerprint = JSON.stringify(snapshot());
    dirty = false;
    history.replaceState(null, '', `${baseUrl}/${state.id}`);
    refreshControls();
  };
  const operation = async action => {
    if (busy) return;
    busy = true;
    message.hidden = true;
    refreshControls();
    try { await action(); } catch (error) { showMessage(error.message, true); }
    finally { busy = false; refreshControls(); }
  };
  const persistDraft = async () => {
    if (!dirty && state) return;
    const content = snapshot();
    const result = state
      ? await request(`${apiUrl}/${state.id}/save`, {expected_version: state.latest.version, snapshot: content})
      : await request(`${apiUrl}/new`, {slug: form.elements.slug.value, snapshot: content});
    updateState(result);
  };
  save.addEventListener('click', () => {
    if (!form.reportValidity()) return;
    operation(async () => {
      await persistDraft();
      showMessage('Draft saved.');
    });
  });
  if (submit) submit.addEventListener('click', () => {
    if (!form.reportValidity()) return;
    operation(async () => { await persistDraft(); updateState(await request(`${apiUrl}/${state.id}/submit`, {expected_version: state.latest.version})); showMessage('Submitted for review.'); });
  });
  if (cancelSubmission) cancelSubmission.addEventListener('click', () => {
    if (!state || dirty || !confirm(`Cancel the submission of V${state.latest.version}?`)) return;
    operation(async () => { updateState(await request(`${apiUrl}/${state.id}/cancel-submission`, {expected_version: state.latest.version})); showMessage('Submission cancelled. The draft has been kept.'); });
  });
  for (const [button, action, notice] of [[requestChanges, 'changes_requested', 'Returned to the author for changes.'], [reject, 'rejected', 'Submission rejected.']]) {
    if (button) button.addEventListener('click', () => {
      const feedback = document.getElementById('article-review-feedback').value.trim();
      if (!state || dirty) return;
      if (!feedback) { showMessage('Please enter review feedback.', true); return; }
      operation(async () => { updateState(await request(`${apiUrl}/${state.id}/review`, {expected_version: state.latest.version, action, feedback})); showMessage(notice); });
    });
  }
  if (publish) publish.addEventListener('click', () => {
    const freeze = state?.latest.snapshot.publication_type === 'sponsored' && !state.order;
    if (dirty || !state || !confirm(`${freeze ? 'Approve and freeze' : 'Publish'} saved version V${state.latest.version}?`)) return;
    operation(async () => {
      updateState(await request(`${apiUrl}/${state.id}/${freeze ? 'approve-sponsored' : 'publish'}`, {expected_version: state.latest.version}));
      showMessage(freeze ? 'Approved and frozen. Awaiting author confirmation.' : 'Article published.');
    });
  });
  if (withdraw) withdraw.addEventListener('click', () => {
    if (!state || !confirm(dirty ? 'Withdraw the current public article? Unsaved changes will be discarded.' : 'Withdraw the current public article?')) return;
    operation(async () => { updateState(await request(`${apiUrl}/${state.id}/withdraw`, {expected_version: state.latest.version})); showMessage('Article withdrawn.'); });
  });
  const setMode = preview => {
    editTab.setAttribute('aria-selected', String(!preview)); previewTab.setAttribute('aria-selected', String(preview));
    editPanel.hidden = preview; previewPanel.hidden = !preview;
  };
  editTab.addEventListener('click', () => setMode(false));
  previewTab.addEventListener('click', () => {
    if (!form.reportValidity()) return;
    operation(async () => {
      const preview = await request(`${baseUrl}/preview`, {snapshot: snapshot()});
      document.getElementById('article-preview-title').textContent = preview.title;
      document.getElementById('article-preview-summary').textContent = preview.summary;
      document.getElementById('article-preview-author').textContent = preview.author;
      document.getElementById('article-preview-body').innerHTML = preview.html;
      const cover = document.getElementById('article-preview-cover');
      const image = document.getElementById('article-preview-image');
      cover.hidden = !preview.cover_id;
      if (preview.cover_id) { image.src = `${baseUrl}/media/${preview.cover_id}`; image.alt = preview.title; }
      else image.removeAttribute('src');
      setMode(true);
    });
  });
  const fileInput = document.getElementById('article-image-file');
  document.getElementById('article-upload').addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', () => {
    const file = fileInput.files[0];
    if (!file) return;
    if (file.size > 2 * 1024 * 1024) { showMessage('The image exceeds 2 MiB.', true); fileInput.value = ''; return; }
    operation(async () => {
      const media = await request(`${baseUrl}/media`, file, true);
      const markdown = form.elements.markdown;
      const alt = file.name.replace(/[\[\]\\<>]/g, '').slice(0, 120);
      markdown.setRangeText(`\n\n![${alt}](/articles/media/${media.id})\n`, markdown.selectionStart, markdown.selectionEnd, 'end');
      const option = document.createElement('option'); option.value = media.id; option.textContent = alt;
      form.elements.cover_id.append(option);
      markDirty(); setMode(false); showMessage('Image uploaded. The article body has not been saved yet.'); fileInput.value = '';
    });
  });
  refreshControls();
})();