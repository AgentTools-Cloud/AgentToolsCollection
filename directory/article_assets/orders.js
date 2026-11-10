(() => {
  const root = document.getElementById('article-order');
  if (!root) return;
  const order = JSON.parse(document.getElementById('article-order-data').textContent);
  const form = document.getElementById('article-order-form');
  const message = document.getElementById('article-order-message');
  const accept = document.getElementById('article-accept-terms');
  const confirmOrder = document.getElementById('article-order-confirm');
  const initialDisabled = new Map(Array.from(form.elements, element => [element, element.disabled]));
  let busy = false;
  const errors = {
    article_payment_creation_pending: 'Invoice creation requires review. No new invoice was created.',
    payment_configuration_changed: 'Payment configuration changed. This order requires review.',
    payment_provider_unavailable: 'The payment provider is unavailable. The existing order has been kept.',
    payment_underpaid: 'Payment requires review.', payment_currency_unavailable: 'This currency is unavailable.',
    payment_network_mismatch: 'The payment network does not match the approved configuration.',
    payment_below_minimum: 'The order is below the payment provider minimum.',
    article_order_not_cancellable: 'This order can no longer be cancelled.',
    article_order_confirmation_conflict: 'The frozen order changed. Please reload before confirming.',
  };
  const controls = () => {
    for (const element of form.elements) element.disabled = busy || initialDisabled.get(element);
    if (confirmOrder) confirmOrder.disabled = busy || !accept.checked;
  };
  if (accept) accept.addEventListener('change', controls);
  form.addEventListener('submit', event => event.preventDefault());
  const act = async (action, body) => {
    if (busy || (action !== 'cancel' && !form.reportValidity())) return;
    busy = true;
    message.hidden = true;
    controls();
    try {
      const response = await fetch(`${root.dataset.baseUrl}/${order.id}/${action}`, {
        method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(errors[result.detail] || result.detail || 'The operation failed.');
      location.reload();
    } catch (error) {
      message.textContent = error.message;
      message.hidden = false;
      message.classList.add('error');
    } finally {
      busy = false;
      controls();
    }
  };
  if (confirmOrder) confirmOrder.addEventListener('click', () => {
    if (accept.checked) act('confirm', {confirmation_hash: order.confirmation_hash, accept_terms: true});
  });
  document.getElementById('article-order-checkout')?.addEventListener('click', () =>
    act('checkout', {pay_currency: document.getElementById('article-pay-currency').value}));
  document.getElementById('article-order-cancel')?.addEventListener('click', () => {
    if (confirm('Cancel this publication order?')) act('cancel', {});
  });
  document.getElementById('article-order-refresh')?.addEventListener('click', () => {
    const identifier = document.getElementById('article-payment-id')?.value.trim();
    act('refresh', identifier ? {payment_id: identifier} : {});
  });
})();