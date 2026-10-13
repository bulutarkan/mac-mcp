import React from "react";
import { Composition } from "remotion";
import { MacMcpDemo } from "./scenes";
import { FPS, seconds } from "./theme";

export const RemotionRoot: React.FC = () => (
  <Composition id="MacMcpDemo" component={MacMcpDemo} durationInFrames={seconds(40)} fps={FPS} width={1920} height={1080} />
);
