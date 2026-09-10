import { StrictMode, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ArrowUp, ChevronLeft, FlaskConical, RotateCcw, Sparkles } from 'lucide-react';
import { Conversation } from './conversation.tsx';
import { previewScenario, type Scenario } from './chat-samples.ts';
import type { DecisionReceipt } from './timeline.ts';
import type { Json } from './protocol.ts';
import './styles.css';
import './chat.css';
import './preview.css';

function Preview() {
  const [scenario, setScenario] = useState<Scenario>('approval');
  const [revision, setRevision] = useState(0);
  const [sample, setSample] = useState(() => previewScenario('approval'));
  const [receipts, setReceipts] = useState<DecisionReceipt[]>([]);
  const choose = (name: Scenario) => { setScenario(name); setSample(previewScenario(name)); setReceipts([]); setRevision(n => n + 1); };
  const respond = async (responses: Json) => {
    setReceipts([{ id: 'preview-decision', afterMessage: sample.messages.at(-1)?.id, interrupts: sample.interrupts, responses }]);
    const rejected = Object.values(responses).some((r: Json) => r.decisions?.some((d: Json) => d.type === 'reject'));
    setSample(old => ({ ...old, running: false, interrupts: [], messages: [...old.messages, { id: 'preview-answer', type: 'ai', content: rejected ? 'I’ll leave the file as it is. We can discuss the changes here before proceeding.' : scenario === 'question' ? 'Thanks. I’ll use your answer to guide the next step.' : 'Your decision was received in this preview. In a real conversation, the agent would now resume the requested action.' }] }));
  };
  return <div className="preview-shell"><header className="preview-header"><a href="/"><ChevronLeft size={15} />Workspace</a><span><FlaskConical size={15} />Chat design preview</span><button onClick={() => choose(scenario)} title="Reset example"><RotateCcw size={15} />Reset</button></header>
    <div className="preview-toolbar"><div><span className="preview-eyebrow">A clearer conversation</span><h1>Follow the work. Stay in control.</h1><p>Interactive examples with sample data. Nothing here runs an agent or changes your files.</p></div><nav aria-label="Example scenario">{(['working', 'approval', 'question', 'complete', 'failure'] as Scenario[]).map(name => <button key={name} aria-pressed={scenario === name} className={scenario === name ? 'active' : ''} onClick={() => choose(name)}>{({ working: 'Working', approval: 'Approval', question: 'Question', complete: 'Completed', failure: 'Failure' })[name]}</button>)}</nav></div>
    <main className="preview-conversation" key={revision}><Conversation scope={'preview-' + revision} messages={sample.messages} interrupts={sample.interrupts} running={sample.running} receipts={receipts} submit={respond} /></main>
    <footer className="preview-composer"><div><span>Ask a follow-up, or type / for skills…</span><button disabled aria-label="Preview composer is disabled"><ArrowUp size={17} /></button></div><small><Sparkles size={11} />Example conversation · Open the workspace to talk to your agent</small></footer>
  </div>;
}
createRoot(document.getElementById('root')!).render(<StrictMode><Preview /></StrictMode>);
