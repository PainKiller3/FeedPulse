/**
 * FeedPulse - Telegram Web App Bridge
 * Handles initialization, theme sync, haptics, and secure authentication tokens.
 */

const TelegramBridge = (function () {
  const tg = window.Telegram?.WebApp;
  const isInsideTelegram = Boolean(tg && tg.initData);

  function init() {
    if (isInsideTelegram) {
      try {
        tg.ready();
        tg.expand();
        // Set header color to match dark aesthetic
        tg.setHeaderColor?.('#080c14');
        tg.setBackgroundColor?.('#080c14');
        
        // Add class to body if Telegram theme is active
        document.body.classList.add('tg-theme');
      } catch (err) {
        console.warn('Error configuring Telegram WebApp SDK:', err);
      }
    }
  }

  function getUser() {
    if (isInsideTelegram && tg.initDataUnsafe?.user) {
      return tg.initDataUnsafe.user;
    }
    return null;
  }

  function getAuthHeaders() {
    const headers = { 'Content-Type': 'application/json' };
    if (isInsideTelegram && tg.initData) {
      headers['Authorization'] = `tma ${tg.initData}`;
      headers['X-Telegram-Init-Data'] = tg.initData;
    }
    return headers;
  }

  // Haptic feedback methods (tactile response on mobile)
  function hapticImpact(style = 'light') {
    if (tg?.HapticFeedback) {
      try {
        tg.HapticFeedback.impactOccurred(style);
      } catch (_) {}
    }
  }

  function hapticNotification(type = 'success') {
    if (tg?.HapticFeedback) {
      try {
        tg.HapticFeedback.notificationOccurred(type);
      } catch (_) {}
    }
  }

  function close() {
    if (isInsideTelegram) {
      try {
        tg.close();
      } catch (_) {}
    }
  }

  return {
    init,
    isInsideTelegram,
    getUser,
    getAuthHeaders,
    hapticImpact,
    hapticNotification,
    close,
    raw: tg,
  };
})();

// Auto-run on load
document.addEventListener('DOMContentLoaded', TelegramBridge.init);
