import tailwindcss from "@tailwindcss/vite";

export default defineNuxtConfig({
  ssr: false,
  compatibilityDate: "2026-08-19",
  app: {
    baseURL: "/pokereye/",
    head: {
      title: "PokerEye",
      meta: [{ name: "viewport", content: "width=device-width, initial-scale=1" }],
    },
  },
  css: ["~/assets/css/main.css"],
  vite: {
    plugins: [tailwindcss()],
  },
  nitro: {
    prerender: { crawlLinks: false, routes: ["/"] },
  },
});
