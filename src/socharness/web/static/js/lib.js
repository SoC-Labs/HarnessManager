// The one place the UI imports its component library from (Preact + htm, vendored).
import { h, render, Fragment } from "../vendor/preact/preact.module.js";
import {
  useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState,
} from "../vendor/preact/hooks.module.js";
import htm from "../vendor/htm/htm.module.js";

export const html = htm.bind(h);
export { Fragment, h, render, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState };
