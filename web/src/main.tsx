import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App.tsx';
import './styles.css';
import './chat.css';
import './appearance.css';

createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>);
