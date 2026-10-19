/**
 * FeedPulse - Telegram Web App Bridge
 * Handles initialization, theme sync, haptics, and secure authentication tokens.
 */

const TelegramBridge = (function () {
  function getTg() {
    return window.Telegram?.WebApp || null;
  }

  function isInsideTelegram() {
    const tg = getTg();
    return Boolean(tg && (tg.initData || tg.initDataUnsafe?.user));
  }

  function init() {
    const tg = getTg();
    if (tg) {
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
    const tg = getTg();
    if (tg?.initDataUnsafe?.user) {
      return tg.initDataUnsafe.user;
    }
    return null;
  }

  function getAuthHeaders() {
    const tg = getTg();
    const headers = { 'Content-Type': 'application/json' };
    if (tg?.initData) {
      headers['Authorization'] = `tma ${tg.initData}`;
      headers['X-Telegram-Init-Data'] = tg.initData;
    }
    if (tg?.initDataUnsafe?.user?.id) {
      headers['X-User-Id'] = String(tg.initDataUnsafe.user.id);
    }
    return headers;
  }

  // Haptic feedback methods (tactile response on mobile)
  function hapticImpact(style = 'light') {
    const tg = getTg();
    if (tg?.HapticFeedback) {
      try {
        tg.HapticFeedback.impactOccurred(style);
      } catch (_) {}
    }
  }

  function hapticNotification(type = 'success') {
    const tg = getTg();
    if (tg?.HapticFeedback) {
      try {
        tg.HapticFeedback.notificationOccurred(type);
      } catch (_) {}
    }
  }

  function close() {
    const tg = getTg();
    if (tg) {
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
    get raw() {
      return getTg();
    },
  };
})();

// Auto-run on load
document.addEventListener('DOMContentLoaded', TelegramBridge.init);
