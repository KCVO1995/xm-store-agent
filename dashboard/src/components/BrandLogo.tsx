import { BRAND_LOGO_SRC, BRAND_NAME } from "../assets/brand";

interface BrandLogoProps {
  size?: number;
  showName?: boolean;
}

export default function BrandLogo({
  size = 36,
  showName = true,
}: BrandLogoProps) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 10,
        flexShrink: 0,
        color: "var(--fn-text-primary)",
        fontSize: Math.round(size * 0.65),
        fontWeight: 700,
        lineHeight: 1,
      }}
    >
      <img
        src={BRAND_LOGO_SRC}
        alt={showName ? "" : BRAND_NAME}
        width={size}
        height={size}
        draggable={false}
        style={{ objectFit: "contain", display: "block" }}
      />
      {showName && <span>{BRAND_NAME}</span>}
    </span>
  );
}
