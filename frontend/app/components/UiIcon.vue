<script setup lang="ts">
defineProps<{ name: string; size?: number }>();
const paths: Record<string, string> = {
  grid: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
  plus: "M12 5v14 M5 12h14",
  users:
    "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2 M9 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M22 21v-2a4 4 0 0 0-3-3.87 M16 3.13a4 4 0 0 1 0 7.75",
  tasks: "M9 5h11 M9 12h11 M9 19h11 M3 5h1 M3 12h1 M3 19h1",
  mail: "M4 4h16a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2 M22 6l-10 7L2 6",
  key: "M15 2a7 7 0 1 1-6.3 10L2 19v3h4v-3h3v-3l2-2 M16 7h.01",
  gift: "M3 8h18v4H3z M5 12v9h14v-9 M12 8v13 M12 8H7a3 3 0 1 1 3-3z M12 8h5a3 3 0 1 0-3-3z",
  settings:
    "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M12 2v3 M12 19v3 M2 12h3 M19 12h3 M5 5l2 2 M17 17l2 2 M5 19l2-2 M17 7l2-2",
  link: "M10 13a5 5 0 0 0 7 .5l4-4a5 5 0 0 0-7-7l-2 2 M14 11a5 5 0 0 0-7-.5l-4 4a5 5 0 0 0 7 7l2-2",
  logout: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4 M16 17l5-5-5-5 M21 12H9",
  refresh:
    "M20 7v5h-5 M4 17v-5h5 M6 7a7 7 0 0 1 12-2l2 3 M18 17a7 7 0 0 1-12 2l-2-3",
  search: "M21 21l-5-5 M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14",
  check: "M5 12l4 4L19 6",
  x: "M6 6l12 12 M6 18L18 6",
  chevron: "M9 5l7 7-7 7",
  menu: "M4 6h16 M4 12h16 M4 18h16",
  download: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4 M7 10l5 5 5-5 M12 15V3",
  upload: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4 M7 8l5-5 5 5 M12 3v12",
  clock: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18 M12 7v5l3 2",
  arrow: "M5 12h14 M13 6l6 6-6 6",
  alert:
    "M12 9v4 M12 17h.01 M10 3L2 18a2 2 0 0 0 2 3h16a2 2 0 0 0 2-3L14 3a2 2 0 0 0-4 0",
  shield: "M12 2l9 4v6c0 5-9 10-9 10S3 17 3 12V6z M8 12l3 3 5-6",
  more: "M5 12h.01 M12 12h.01 M19 12h.01",
};
</script>
<template>
  <svg
    :width="size || 18"
    :height="size || 18"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.7"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <path :d="paths[name] || paths.grid" />
  </svg>
</template>
