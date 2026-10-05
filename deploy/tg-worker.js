// Релей Telegram Bot API для CRM (VM в России, api.telegram.org недоступен).
//
// Это Cloudflare Worker: живёт на краю сети Cloudflare, работает 24/7
// бесплатно, без сервера. Он просто пересылает запросы вида
//   https://твой-воркер.workers.dev/bot<ТОКЕН>/<метод>
// на https://api.telegram.org/bot<ТОКЕН>/<метод> — ничего не хранит,
// токен остаётся секретом (он и есть авторизация).
//
// Установка: Cloudflare -> Workers & Pages -> Create Worker -> Edit code
// -> заменить шаблон ЭТИМ кодом -> Deploy. Скопировать URL воркера.
//
// Защита от чужих: пропускаем только пути /bot... — посторонний не сможет
// использовать воркер как прокси для своих нужд.

const UPSTREAM = "https://api.telegram.org";

export default {
  async fetch(request) {
    const url = new URL(request.url);

    if (!url.pathname.startsWith("/bot")) {
      return new Response(JSON.stringify({ ok: false, error: "не Telegram-путь" }), {
        status: 403,
        headers: { "Content-Type": "application/json" },
      });
    }

    const upstream = UPSTREAM + url.pathname + url.search;
    const init = {
      method: request.method,
      headers: {
        "Content-Type": request.headers.get("Content-Type") || "application/json",
      },
    };
    if (request.method === "POST") {
      init.body = request.body;
    }

    const response = await fetch(upstream, init);
    return new Response(response.body, {
      status: response.status,
      headers: {
        "Content-Type": response.headers.get("Content-Type") || "application/json",
      },
    });
  },
};
