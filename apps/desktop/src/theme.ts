import type { ThemeConfig } from "antd";

/**
 * Design tokens lifted from prototypes/zooagent-redesign.html.
 *
 * The product is meant to feel warm and human, not like a generic admin
 * console: paper background, deep green ink, a lime accent and soft borders.
 * Keep this in sync with the CSS custom properties in styles.css.
 */
export const palette = {
  ink: "#17261f",
  muted: "#78847d",
  line: "#e7ece8",
  paper: "#fbfcf9",
  green: "#1f7556",
  greenDeep: "#173b2c",
  lime: "#d5f36a",
  mint: "#eef5ee",
  violet: "#7865db",
  warm: "#faf0e4",
};

export const FONT_STACK =
  'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif';

export const zooTheme: ThemeConfig = {
  token: {
    colorPrimary: palette.green,
    colorInfo: palette.green,
    colorSuccess: "#4f9a6e",
    colorLink: palette.green,
    colorText: palette.ink,
    colorTextHeading: palette.ink,
    colorTextSecondary: palette.muted,
    colorBorder: palette.line,
    colorBorderSecondary: palette.line,
    colorBgLayout: palette.paper,
    colorBgContainer: "#ffffff",
    borderRadius: 10,
    fontFamily: FONT_STACK,
    fontSize: 14,
  },
  components: {
    Button: { primaryShadow: "none", defaultShadow: "none", fontWeight: 600 },
    Card: { colorBorderSecondary: palette.line, headerFontSize: 14 },
    Table: {
      headerBg: "#f3f7f3",
      headerColor: "#4b5a52",
      borderColor: palette.line,
      rowHoverBg: "#f6faf6",
    },
    Segmented: {
      itemSelectedBg: "#e3efe6",
      itemSelectedColor: palette.green,
      trackBg: "#f1f4f0",
    },
    Tabs: { inkBarColor: palette.green, itemSelectedColor: palette.green, itemColor: palette.muted },
    Modal: { titleFontSize: 17 },
    Tag: { defaultBg: "#f1f5f1", defaultColor: "#4b5a52" },
    Statistic: { contentFontSize: 26 },
  },
};

/** Chart palette: green-forward, with warm and violet accents. */
export const CHART_COLORS = [
  palette.green,
  "#5d9c76",
  "#a5cc8c",
  palette.lime,
  "#e5ad77",
  palette.violet,
  "#4f8a8b",
  "#c98b8b",
];
