export function RoadTripLogoSVG({
  className,
  width,
  height,
}: {
  width?: number;
  height?: number;
  className?: string;
}) {
  return (
    <svg
      width={width}
      height={height}
      viewBox="0 0 64 64"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
      role="img"
      aria-label="Road trip planner"
    >
      {/* sun */}
      <circle cx="44" cy="18" r="7" className="fill-accent" />
      {/* far hills */}
      <path
        d="M2 44c8-9 16-13 22-13s10 3 14 7 9 6 12 6 10-3 14-8v22H2v-14Z"
        className="fill-primary/25"
      />
      {/* near hills */}
      <path
        d="M2 50c7-6 13-9 18-9 6 0 10 3 14 7s10 6 15 6c6 0 11-3 15-8v18H2V50Z"
        className="fill-primary/45"
      />
      {/* road */}
      <path
        d="M20 64c0-12 4-19 12-19s12 7 12 19H20Z"
        className="fill-primary"
      />
      {/* centre dashes */}
      <path
        d="M32 47.5c.9 0 1.5.7 1.4 1.6l-.5 5.2c-.06.8-.7 1.4-1.4 1.4s-1.35-.6-1.4-1.4l-.5-5.2c-.1-.9.5-1.6 1.4-1.6Z"
        className="fill-background"
      />
      <path
        d="M32 58.2c.75 0 1.3.6 1.25 1.35l-.1 2.6c-.04.7-.6 1.25-1.25 1.25s-1.2-.55-1.25-1.25l-.1-2.6c-.05-.75.5-1.35 1.25-1.35Z"
        className="fill-background"
      />
    </svg>
  );
}
