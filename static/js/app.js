(() => {
  const chatLog = document.getElementById("chat-log");
  const form = document.getElementById("message-form");
  const input = document.getElementById("message-input");
  const sendButton = document.getElementById("send-button");
  const welcome = document.getElementById("welcome-panel");
  const errorBanner = document.getElementById("error-banner");
  const conversationList = document.getElementById("conversation-list");
  const newButton = document.getElementById("new-conversation-button");
  const closeButton = document.getElementById("close-conversation-button");
  const heading = document.getElementById("conversation-heading");
  const stateLabel = document.getElementById("conversation-state");

  // Milestone 3 UI elements: Auth & Alerts
  const authStatusBtn = document.getElementById("auth-status-btn");
  const openAlertsBtn = document.getElementById("open-alerts-btn");
  const authModal = document.getElementById("auth-modal");
  const closeAuthModal = document.getElementById("close-auth-modal");
  const alertsModal = document.getElementById("alerts-modal");
  const closeAlertsModal = document.getElementById("close-alerts-modal");

  const authError = document.getElementById("auth-error");
  const authStepRequest = document.getElementById("auth-step-request");
  const authStepVerify = document.getElementById("auth-step-verify");
  const authStepProfile = document.getElementById("auth-step-profile");
  const authRequestForm = document.getElementById("auth-request-form");
  const authEmailInput = document.getElementById("auth-email-input");
  const authVerifyForm = document.getElementById("auth-verify-form");
  const authCodeInput = document.getElementById("auth-code-input");
  const authVerifyDesc = document.getElementById("auth-verify-desc");
  const authBackBtn = document.getElementById("auth-back-btn");
  const authLogoutBtn = document.getElementById("auth-logout-btn");
  const profileUserHeading = document.getElementById("profile-user-heading");
  const profileMeta = document.getElementById("profile-meta");

  const alertsError = document.getElementById("alerts-error");
  const alertsSuccess = document.getElementById("alerts-success");
  const subscribeForm = document.getElementById("subscribe-form");
  const subTrackingInput = document.getElementById("sub-tracking-input");
  const subChannelSelect = document.getElementById("sub-channel-select");
  const subRecipientGroup = document.getElementById("sub-recipient-group");
  const subRecipientInput = document.getElementById("sub-recipient-input");
  const subConsentCheckbox = document.getElementById("sub-consent-checkbox");
  const telegramLinkBanner = document.getElementById("telegram-link-banner");
  const telegramLinkBtn = document.getElementById("telegram-link-btn");
  const subsContainer = document.getElementById("subscriptions-list-container");

  let ready = false;
  let sending = false;
  let currentConversationId = null;
  let currentStatus = "active";
  let conversations = [];
  let currentAuthUser = null;
  let activeChallengeId = null;

  function showError(message) { errorBanner.textContent = message; errorBanner.hidden = false; }
  function clearError() { errorBanner.textContent = ""; errorBanner.hidden = true; }

  function addMessage(role, content, pending = false) {
    const row = document.createElement("div");
    row.className = `message ${role}${pending ? " pending" : ""}`;
    const avatar = document.createElement("div");
    avatar.className = "avatar";
    avatar.textContent = role === "user" ? "YOU" : "IP";
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = content;
    row.append(avatar, bubble);
    chatLog.appendChild(row);
    row.scrollIntoView({ block: "nearest", behavior: "smooth" });
    return { row, bubble };
  }

  function setBusy(value) {
    sending = value;
    sendButton.disabled = value || !ready || currentStatus !== "active";
    input.disabled = value || !ready || currentStatus !== "active";
    newButton.disabled = value;
    closeButton.disabled = value || currentStatus !== "active";
    sendButton.querySelector("span:first-child").textContent = value ? "Sending" : "Send";
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    let data = {};
    try { data = await response.json(); } catch (_) { /* handled below */ }
    if (!response.ok) {
      const detail = typeof data.detail === "string" ? data.detail : "The local application request failed.";
      throw new Error(detail);
    }
    return data;
  }

  function titleFor(item) {
    return (item.first_message || "New conversation").replace(/\s+/g, " ").trim().slice(0, 64) || "New conversation";
  }

  function renderHistory() {
    conversationList.replaceChildren();
    if (!conversations.length) {
      const empty = document.createElement("p");
      empty.className = "history-empty";
      empty.textContent = "Your conversations will appear here.";
      conversationList.appendChild(empty);
      return;
    }
    conversations.forEach((item) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `history-item${item.conversation_id === currentConversationId ? " selected" : ""}`;
      const title = document.createElement("span");
      title.className = "history-title";
      title.textContent = titleFor(item);
      const meta = document.createElement("span");
      meta.className = "history-meta";
      meta.textContent = item.status === "closed" ? "Closed · History retained" : "Active";
      button.append(title, meta);
      button.addEventListener("click", () => openConversation(item.conversation_id));
      conversationList.appendChild(button);
    });
  }

  function updateControls() {
    heading.textContent = currentStatus === "closed" ? "Closed conversation" : (conversations.find(x => x.conversation_id === currentConversationId)?.first_message || "New conversation").slice(0, 64);
    stateLabel.textContent = currentStatus === "closed" ? "Read-only · History retained" : "Active conversation";
    setBusy(false);
  }

  async function refreshHistory() {
    const data = await api("/api/conversations", { method: "GET", headers: {} });
    conversations = data.conversations || [];
    renderHistory();
    updateControls();
  }

  function displayMessages(messages) {
    chatLog.replaceChildren();
    (messages || []).forEach(message => addMessage(message.role, message.content));
    welcome.hidden = (messages || []).length > 0;
  }

  async function initialise() {
    try {
      const data = await api("/api/session", { method: "GET", headers: {} });
      currentConversationId = data.conversation_id;
      currentStatus = "active";
      displayMessages(data.messages);
      conversations = data.conversations || [];
      ready = true;
      renderHistory();
      updateControls();
      input.focus();
    } catch (error) {
      showError(`Could not initialise the local session: ${error.message}`);
    }
  }

  async function newConversation() {
    if (sending) return;
    clearError();
    try {
      const data = await api("/api/conversations", { method: "POST", body: "{}" });
      currentConversationId = data.conversation_id;
      currentStatus = "active";
      displayMessages([]);
      await refreshHistory();
      input.focus();
    } catch (error) { showError(error.message); }
  }

  async function openConversation(conversationId) {
    if (sending || conversationId === currentConversationId) return;
    clearError();
    try {
      const data = await api(`/api/conversations/${encodeURIComponent(conversationId)}`, { method: "GET", headers: {} });
      currentConversationId = data.conversation_id;
      currentStatus = data.status;
      displayMessages(data.messages);
      renderHistory();
      updateControls();
      if (currentStatus === "active") input.focus();
    } catch (error) { showError(error.message); }
  }

  async function closeConversation() {
    if (sending || currentStatus !== "active") return;
    if (!window.confirm("Close this conversation? It will remain in your history and will not be deleted.")) return;
    clearError();
    try {
      const data = await api(`/api/conversations/${encodeURIComponent(currentConversationId)}/close`, { method: "POST", body: "{}" });
      currentConversationId = data.conversation_id;
      currentStatus = "active";
      displayMessages([]);
      await refreshHistory();
      input.focus();
    } catch (error) { showError(error.message); }
  }

  async function sendMessage(text, requestId = crypto.randomUUID(), addUser = true) {
    if (!ready || sending || currentStatus !== "active" || !text.trim()) return;
    clearError();
    welcome.hidden = true;
    if (addUser) addMessage("user", text.trim());
    const pending = addMessage("assistant", "Preparing your response…", true);
    setBusy(true);
    try {
      const data = await api("/api/messages", {
        method: "POST",
        body: JSON.stringify({ message: text.trim(), request_id: requestId }),
      });
      pending.bubble.textContent = data.response;
      pending.row.classList.remove("pending");
      await refreshHistory();
    } catch (error) {
      pending.bubble.textContent = `${error.message} `;
      pending.row.classList.remove("pending");
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "retry-button";
      retry.textContent = "Retry same request";
      retry.addEventListener("click", () => {
        pending.row.remove();
        sendMessage(text, requestId, false);
      }, { once: true });
      pending.bubble.appendChild(retry);
      showError("If the previous response was completed but the connection was interrupted, retrying with the same request ID safely retrieves the stored response.");
    } finally {
      setBusy(false);
      if (currentStatus === "active") { input.value = ""; input.focus(); }
    }
  }

  // -------------------------------------------------------------------------
  // Milestone 3: Passwordless Authentication Logic
  // -------------------------------------------------------------------------

  function showAuthError(msg) { authError.textContent = msg; authError.hidden = false; }
  function clearAuthError() { authError.textContent = ""; authError.hidden = true; }

  async function checkAuthState() {
    try {
      const data = await api("/api/auth/me", { method: "GET", headers: {} });
      currentAuthUser = data;
      authStatusBtn.textContent = `User #${data.user_id}`;
      authStatusBtn.title = "View profile / sign out";
    } catch (_) {
      currentAuthUser = null;
      authStatusBtn.textContent = "Sign In";
      authStatusBtn.title = "Sign in as a registered user";
    }
  }

  function openAuth() {
    clearAuthError();
    if (currentAuthUser) {
      authStepRequest.hidden = true;
      authStepVerify.hidden = true;
      authStepProfile.hidden = false;
      profileUserHeading.textContent = `Registered User #${currentAuthUser.user_id}`;
      profileMeta.textContent = `Session valid until ${currentAuthUser.expires_at || "expiry"}`;
    } else {
      authStepRequest.hidden = false;
      authStepVerify.hidden = true;
      authStepProfile.hidden = true;
      authEmailInput.value = "";
      authCodeInput.value = "";
    }
    authModal.hidden = false;
  }

  authRequestForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    clearAuthError();
    const email = authEmailInput.value.trim();
    if (!email) return;
    try {
      const res = await api("/api/auth/challenge", {
        method: "POST",
        body: JSON.stringify({ email }),
      });
      activeChallengeId = res.challenge_id;
      authVerifyDesc.textContent = `A 6-digit code has been sent to ${email}.`;
      authStepRequest.hidden = true;
      authStepVerify.hidden = false;
      authCodeInput.focus();
    } catch (err) {
      showAuthError(err.message);
    }
  });

  authVerifyForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    clearAuthError();
    const code = authCodeInput.value.trim();
    if (!activeChallengeId || !code) return;
    try {
      const res = await api("/api/auth/verify", {
        method: "POST",
        body: JSON.stringify({ challenge_id: activeChallengeId, code }),
      });
      currentAuthUser = res;
      await checkAuthState();
      authStepVerify.hidden = true;
      authStepProfile.hidden = false;
      profileUserHeading.textContent = `Registered User #${res.user_id}`;
      profileMeta.textContent = `Email: ${res.email}`;
    } catch (err) {
      showAuthError(err.message);
    }
  });

  authBackBtn.addEventListener("click", () => {
    clearAuthError();
    authStepVerify.hidden = true;
    authStepRequest.hidden = false;
  });

  authLogoutBtn.addEventListener("click", async () => {
    clearAuthError();
    try {
      await api("/api/auth/logout", { method: "POST", body: "{}" });
    } catch (_) { /* ignore */ }
    currentAuthUser = null;
    await checkAuthState();
    authModal.hidden = true;
  });

  closeAuthModal.addEventListener("click", () => { authModal.hidden = true; });

  // -------------------------------------------------------------------------
  // Milestone 3: Tracking Alerts & Subscriptions Logic
  // -------------------------------------------------------------------------

  function showAlertsError(msg) { alertsError.textContent = msg; alertsError.hidden = false; alertsSuccess.hidden = true; }
  function showAlertsSuccess(msg) { alertsSuccess.textContent = msg; alertsSuccess.hidden = false; alertsError.hidden = true; }
  function clearAlertsMessages() { alertsError.hidden = true; alertsSuccess.hidden = true; }

  async function loadSubscriptions() {
    subsContainer.replaceChildren();
    const loading = document.createElement("p");
    loading.className = "subs-empty";
    loading.textContent = "Loading subscriptions…";
    subsContainer.appendChild(loading);

    try {
      const data = await api("/api/guest/subscriptions", { method: "GET", headers: {} });
      const subs = data.subscriptions || [];
      subsContainer.replaceChildren();

      if (!subs.length) {
        const empty = document.createElement("p");
        empty.className = "subs-empty";
        empty.textContent = "No active consignment subscriptions found for this session.";
        subsContainer.appendChild(empty);
        return;
      }

      subs.forEach((sub) => {
        const item = document.createElement("div");
        item.className = "sub-item";

        const info = document.createElement("div");
        info.className = "sub-info";
        const title = document.createElement("strong");
        title.textContent = sub.tracking_number;
        const meta = document.createElement("span");
        meta.textContent = `${sub.carrier} · Status: ${sub.current_status || "Active"} · Every ${sub.monitoring_interval_hours}h`;

        const badge = document.createElement("span");
        badge.className = `sub-badge${sub.monitoring_enabled ? "" : " disabled"}`;
        badge.textContent = sub.monitoring_enabled ? "Active" : "Disabled";
        title.appendChild(badge);

        info.append(title, meta);

        const unsubBtn = document.createElement("button");
        unsubBtn.type = "button";
        unsubBtn.className = "sub-unsub-btn";
        unsubBtn.textContent = "Unsubscribe";
        unsubBtn.disabled = !sub.monitoring_enabled;
        unsubBtn.addEventListener("click", async () => {
          try {
            await api("/api/guest/unsubscribe", {
              method: "POST",
              body: JSON.stringify({ guest_subscription_id: sub.guest_subscription_id }),
            });
            showAlertsSuccess(`Unsubscribed from tracking for ${sub.tracking_number}.`);
            await loadSubscriptions();
          } catch (err) {
            showAlertsError(err.message);
          }
        });

        item.append(info, unsubBtn);
        subsContainer.appendChild(item);
      });
    } catch (err) {
      subsContainer.replaceChildren();
      showAlertsError(`Failed to load subscriptions: ${err.message}`);
    }
  }

  async function loadTelegramLink() {
    try {
      const data = await api("/api/guest/telegram-link", { method: "POST", body: "{}" });
      if (data && data.deep_link) {
        telegramLinkBtn.href = data.deep_link;
      }
    } catch (_) { /* non-critical */ }
  }

  function openAlerts() {
    clearAlertsMessages();
    alertsModal.hidden = false;
    loadSubscriptions();
    loadTelegramLink();
  }

  closeAlertsModal.addEventListener("click", () => { alertsModal.hidden = true; });

  subChannelSelect.addEventListener("change", () => {
    subRecipientGroup.hidden = subChannelSelect.value === "telegram";
  });

  subscribeForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    clearAlertsMessages();

    const trackingNumber = subTrackingInput.value.trim();
    const channel = subChannelSelect.value;
    const recipient = subRecipientInput.value.trim();
    const consent = subConsentCheckbox.checked;

    if (!trackingNumber) return;
    if (channel === "email" && !recipient) {
      showAlertsError("An email address is required for email notifications.");
      return;
    }

    try {
      const payload = {
        tracking_number: trackingNumber,
        carrier: "india-post",
        channel,
        consent,
      };
      if (channel === "email") {
        payload.recipient = recipient;
      }

      const res = await api("/api/guest/subscribe", {
        method: "POST",
        body: JSON.stringify(payload),
      });

      if (res.requires_telegram_link && res.telegram_deep_link) {
        telegramLinkBtn.href = res.telegram_deep_link;
        showAlertsSuccess(`Subscribed! Click 'Open Telegram' below to complete linking your bot.`);
      } else {
        showAlertsSuccess(`Successfully subscribed to updates for ${trackingNumber}!`);
      }

      subTrackingInput.value = "";
      await loadSubscriptions();
    } catch (err) {
      showAlertsError(err.message);
    }
  });

  authStatusBtn.addEventListener("click", openAuth);
  openAlertsBtn.addEventListener("click", openAlerts);

  // Close modals on clicking backdrop outside card
  window.addEventListener("click", (e) => {
    if (e.target === authModal) authModal.hidden = true;
    if (e.target === alertsModal) alertsModal.hidden = true;
  });

  form.addEventListener("submit", event => { event.preventDefault(); sendMessage(input.value); });
  input.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); form.requestSubmit(); }
  });
  newButton.addEventListener("click", newConversation);
  closeButton.addEventListener("click", closeConversation);
  document.querySelectorAll(".suggestion").forEach(button => button.addEventListener("click", () => sendMessage(button.dataset.prompt || "")));
  
  checkAuthState();
  initialise();
})();
