import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { readLaunchToken } from './api/client';
import './styles/app.css';

const tokenPresent = readLaunchToken();
const root = document.getElementById('root');
if (!root) throw new Error('找不到页面根节点');

createRoot(root).render(
  <StrictMode>
    <App tokenPresent={tokenPresent} />
  </StrictMode>,
);
