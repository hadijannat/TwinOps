import React, { useState, useCallback } from 'react';
import { Play, Square, Gauge, Thermometer, AlertTriangle, Shield, Check, X, Clock, Terminal, Settings, Activity, Lock, Eye, Wrench } from 'lucide-react';

// TwinOps Interactive Demo - Industrial Pump Digital Twin
// This demonstrates the 5-Layer Safety Model and AI Agent interaction

const RISK_COLORS = {
  LOW: '#27ae60',
  MEDIUM: '#f1c40f',
  HIGH: '#e67e22',
  CRITICAL: '#e74c3c'
};

const ROLE_PERMISSIONS = {
  viewer: { allow: ['GetStatus'], color: '#3498db' },
  operator: { allow: ['StartPump', 'StopPump', 'SetSpeed', 'GetStatus'], color: '#2ecc71' },
  maintenance: { allow: ['*'], color: '#9b59b6' }
};

const OPERATIONS = {
  GetStatus: { risk: 'LOW', description: 'Get current pump status', icon: Eye },
  StartPump: { risk: 'HIGH', description: 'Start the pump motor', icon: Play },
  StopPump: { risk: 'HIGH', description: 'Stop the pump motor', icon: Square },
  SetSpeed: { risk: 'HIGH', description: 'Set pump rotational speed', icon: Gauge },
  EmergencyStop: { risk: 'CRITICAL', description: 'Emergency stop - immediately halts all operations', icon: AlertTriangle }
};

export default function TwinOpsDemo() {
  // Pump state (Digital Twin Shadow)
  const [pumpState, setPumpState] = useState({
    state: 'Stopped',
    currentSpeed: 0,
    targetSpeed: 0,
    temperature: 45,
    pressure: 1.0,
    flowRate: 0,
    dischargeValve: 'OPEN'
  });

  // UI State
  const [currentRole, setCurrentRole] = useState('operator');
  const [messages, setMessages] = useState([]);
  const [command, setCommand] = useState('');
  const [pendingApproval, setPendingApproval] = useState(null);
  const [auditLog, setAuditLog] = useState([]);

  // Add message to chat
  const addMessage = useCallback((text, type = 'system', details = null) => {
    const timestamp = new Date().toLocaleTimeString();
    setMessages(prev => [...prev, { text, type, timestamp, details }]);
  }, []);

  // Add audit entry
  const addAuditEntry = useCallback((event, tool, result, role) => {
    const entry = {
      timestamp: new Date().toISOString(),
      event,
      tool,
      result,
      role,
      hash: Math.random().toString(36).substring(7) // Simulated hash chain
    };
    setAuditLog(prev => [...prev, entry]);
  }, []);

  // Safety Layer 1: RBAC Check
  const checkRBAC = useCallback((operation) => {
    const permissions = ROLE_PERMISSIONS[currentRole];
    if (permissions.allow.includes('*') || permissions.allow.includes(operation)) {
      return { allowed: true, reason: `Role '${currentRole}' is authorized for ${operation}` };
    }
    return { allowed: false, reason: `Role '${currentRole}' is NOT authorized for ${operation}` };
  }, [currentRole]);

  // Safety Layer 2: Interlock Check
  const checkInterlocks = useCallback((operation) => {
    // Temperature interlock
    if (pumpState.temperature > 95) {
      return { allowed: false, reason: 'INTERLOCK: Temperature too high (>95°C). Operation blocked.' };
    }
    // Discharge valve interlock for pump start
    if (operation === 'StartPump' && pumpState.dischargeValve === 'CLOSED') {
      return { allowed: false, reason: 'INTERLOCK: Cannot start pump - discharge valve is closed.' };
    }
    return { allowed: true, reason: 'All interlocks passed' };
  }, [pumpState]);

  // Safety Layer 3: Risk Assessment
  const assessRisk = useCallback((operation) => {
    const risk = OPERATIONS[operation]?.risk || 'MEDIUM';
    const forceSimulation = risk === 'HIGH' || risk === 'CRITICAL';
    const requireApproval = risk === 'CRITICAL';
    return { risk, forceSimulation, requireApproval };
  }, []);

  // Execute operation (simulated or real)
  const executeOperation = useCallback((operation, params = {}, simulated = false) => {
    if (simulated) {
      addMessage(`🔬 SIMULATION: ${operation} would ${getOperationEffect(operation, params)}`, 'simulation');
      addAuditEntry('simulated', operation, 'success', currentRole);
      return { success: true, simulated: true };
    }

    // Actually execute
    switch (operation) {
      case 'GetStatus':
        addMessage(`📊 Pump Status: ${pumpState.state}, Speed: ${pumpState.currentSpeed} RPM, Temp: ${pumpState.temperature}°C`, 'result');
        break;
      case 'StartPump':
        setPumpState(prev => ({ ...prev, state: 'Running', currentSpeed: prev.targetSpeed || 1000, flowRate: 50 }));
        addMessage('✅ Pump started successfully', 'success');
        break;
      case 'StopPump':
        setPumpState(prev => ({ ...prev, state: 'Stopped', currentSpeed: 0, flowRate: 0 }));
        addMessage('✅ Pump stopped successfully', 'success');
        break;
      case 'SetSpeed':
        const rpm = params.RPM || 1200;
        setPumpState(prev => ({
          ...prev,
          targetSpeed: rpm,
          currentSpeed: prev.state === 'Running' ? rpm : 0
        }));
        addMessage(`✅ Speed set to ${rpm} RPM`, 'success');
        break;
      case 'EmergencyStop':
        setPumpState(prev => ({ ...prev, state: 'Emergency Stop', currentSpeed: 0, flowRate: 0 }));
        addMessage('🚨 EMERGENCY STOP EXECUTED', 'critical');
        break;
    }
    addAuditEntry('executed', operation, 'success', currentRole);
    return { success: true, simulated: false };
  }, [pumpState, currentRole, addMessage, addAuditEntry]);

  const getOperationEffect = (operation, params) => {
    switch (operation) {
      case 'StartPump': return 'start the pump motor';
      case 'StopPump': return 'stop the pump motor';
      case 'SetSpeed': return `set speed to ${params.RPM || 1200} RPM`;
      case 'EmergencyStop': return 'immediately halt all operations';
      default: return 'query the system';
    }
  };

  // Process natural language command through AI agent
  const processCommand = useCallback((text) => {
    addMessage(text, 'user');

    // Simple NLU - detect intent
    let operation = null;
    let params = {};

    const lowerText = text.toLowerCase();
    if (lowerText.includes('status') || lowerText.includes('how is') || lowerText.includes('what is')) {
      operation = 'GetStatus';
    } else if (lowerText.includes('start')) {
      operation = 'StartPump';
    } else if (lowerText.includes('emergency') || lowerText.includes('e-stop')) {
      operation = 'EmergencyStop';
    } else if (lowerText.includes('stop')) {
      operation = 'StopPump';
    } else if (lowerText.includes('speed') || lowerText.includes('rpm')) {
      operation = 'SetSpeed';
      const match = text.match(/(\d+)/);
      if (match) params.RPM = parseInt(match[1]);
    }

    if (!operation) {
      addMessage("🤖 I couldn't understand that command. Try: 'start pump', 'stop pump', 'set speed to 1200 RPM', 'get status', or 'emergency stop'", 'system');
      return;
    }

    addMessage(`🤖 Interpreting intent: ${operation}`, 'agent');

    // Run through 5-layer safety model
    addMessage('🛡️ Running safety evaluation...', 'system');

    // Layer 1: RBAC
    const rbacResult = checkRBAC(operation);
    addMessage(`  Layer 1 (RBAC): ${rbacResult.allowed ? '✓' : '✗'} ${rbacResult.reason}`, rbacResult.allowed ? 'pass' : 'fail');
    if (!rbacResult.allowed) {
      addAuditEntry('denied', operation, 'rbac_failed', currentRole);
      return;
    }

    // Layer 2: Interlocks
    const interlockResult = checkInterlocks(operation);
    addMessage(`  Layer 2 (Interlocks): ${interlockResult.allowed ? '✓' : '✗'} ${interlockResult.reason}`, interlockResult.allowed ? 'pass' : 'fail');
    if (!interlockResult.allowed) {
      addAuditEntry('denied', operation, 'interlock_failed', currentRole);
      return;
    }

    // Layer 3: Risk Assessment
    const riskAssessment = assessRisk(operation);
    addMessage(`  Layer 3 (Risk): ${riskAssessment.risk} risk detected`, 'info', { risk: riskAssessment.risk });

    // Layer 4: HITL for CRITICAL
    if (riskAssessment.requireApproval) {
      addMessage(`  Layer 4 (HITL): ⏳ CRITICAL operation requires supervisor approval`, 'warning');
      setPendingApproval({ operation, params, taskId: `task-${Date.now()}` });
      addAuditEntry('pending_approval', operation, 'awaiting', currentRole);
      return;
    }

    // Force simulation for HIGH risk
    if (riskAssessment.forceSimulation) {
      addMessage(`  Layer 3 (Simulation): HIGH risk → forcing simulation first`, 'warning');
      executeOperation(operation, params, true);
      addMessage(`  💡 To execute for real, re-issue the command or approve simulation result`, 'info');
      return;
    }

    // Layer 5: Execute and audit
    executeOperation(operation, params, false);
    addMessage(`  Layer 5 (Audit): ✓ Operation logged to tamper-evident audit trail`, 'pass');
  }, [checkRBAC, checkInterlocks, assessRisk, executeOperation, addMessage, currentRole, addAuditEntry]);

  // Handle approval/rejection
  const handleApproval = (approved) => {
    if (!pendingApproval) return;

    if (approved) {
      addMessage(`👤 Supervisor approved ${pendingApproval.operation}`, 'success');
      executeOperation(pendingApproval.operation, pendingApproval.params, false);
      addAuditEntry('approved', pendingApproval.operation, 'success', 'supervisor');
    } else {
      addMessage(`👤 Supervisor rejected ${pendingApproval.operation}`, 'fail');
      addAuditEntry('rejected', pendingApproval.operation, 'denied', 'supervisor');
    }
    setPendingApproval(null);
  };

  // Adjust temperature for demo
  const adjustTemperature = (delta) => {
    setPumpState(prev => ({
      ...prev,
      temperature: Math.max(20, Math.min(100, prev.temperature + delta))
    }));
  };

  // Toggle discharge valve
  const toggleValve = () => {
    setPumpState(prev => ({
      ...prev,
      dischargeValve: prev.dischargeValve === 'OPEN' ? 'CLOSED' : 'OPEN'
    }));
  };

  return (
    <div className="min-h-screen bg-gray-900 text-white p-4">
      <div className="max-w-7xl mx-auto">
        {/* Header */}
        <div className="text-center mb-6">
          <h1 className="text-3xl font-bold mb-2">🏭 TwinOps Interactive Demo</h1>
          <p className="text-gray-400">Production-Grade AI Agents for BaSyx Digital Twins</p>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          {/* Left Panel: Digital Twin State */}
          <div className="bg-gray-800 rounded-lg p-4">
            <h2 className="text-xl font-bold mb-4 flex items-center gap-2">
              <Activity size={20} /> Digital Twin: Pump-001
            </h2>

            {/* Pump Visual */}
            <div className={`rounded-lg p-6 mb-4 text-center ${
              pumpState.state === 'Running' ? 'bg-green-900' :
              pumpState.state === 'Emergency Stop' ? 'bg-red-900' : 'bg-gray-700'
            }`}>
              <div className="text-6xl mb-2">
                {pumpState.state === 'Running' ? '⚙️' : pumpState.state === 'Emergency Stop' ? '🚨' : '⏹️'}
              </div>
              <div className="text-2xl font-bold">{pumpState.state}</div>
              <div className="text-4xl font-mono mt-2">{pumpState.currentSpeed} RPM</div>
            </div>

            {/* Metrics */}
            <div className="space-y-3">
              <div className="flex items-center justify-between bg-gray-700 rounded p-3">
                <span className="flex items-center gap-2"><Thermometer size={16} /> Temperature</span>
                <span className={`font-mono ${pumpState.temperature > 95 ? 'text-red-400' : pumpState.temperature > 80 ? 'text-yellow-400' : 'text-green-400'}`}>
                  {pumpState.temperature}°C
                </span>
              </div>
              <div className="flex items-center justify-between bg-gray-700 rounded p-3">
                <span className="flex items-center gap-2"><Gauge size={16} /> Pressure</span>
                <span className="font-mono">{pumpState.pressure.toFixed(1)} bar</span>
              </div>
              <div className="flex items-center justify-between bg-gray-700 rounded p-3">
                <span>Flow Rate</span>
                <span className="font-mono">{pumpState.flowRate} m³/h</span>
              </div>
              <div className="flex items-center justify-between bg-gray-700 rounded p-3">
                <span>Discharge Valve</span>
                <span className={`font-mono ${pumpState.dischargeValve === 'OPEN' ? 'text-green-400' : 'text-red-400'}`}>
                  {pumpState.dischargeValve}
                </span>
              </div>
            </div>

            {/* Demo Controls */}
            <div className="mt-4 pt-4 border-t border-gray-700">
              <h3 className="text-sm text-gray-400 mb-2">Demo Controls (Simulate Conditions)</h3>
              <div className="flex gap-2 mb-2">
                <button onClick={() => adjustTemperature(-10)} className="flex-1 bg-blue-600 hover:bg-blue-700 rounded px-3 py-2 text-sm">
                  🌡️ -10°C
                </button>
                <button onClick={() => adjustTemperature(10)} className="flex-1 bg-red-600 hover:bg-red-700 rounded px-3 py-2 text-sm">
                  🌡️ +10°C
                </button>
              </div>
              <button onClick={toggleValve} className="w-full bg-gray-600 hover:bg-gray-500 rounded px-3 py-2 text-sm">
                🔧 Toggle Discharge Valve
              </button>
            </div>
          </div>

          {/* Center Panel: Agent Chat */}
          <div className="bg-gray-800 rounded-lg p-4 flex flex-col">
            <h2 className="text-xl font-bold mb-4 flex items-center gap-2">
              <Terminal size={20} /> AI Agent Interface
            </h2>

            {/* Role Selector */}
            <div className="mb-4">
              <label className="text-sm text-gray-400 block mb-2">Current Role:</label>
              <div className="flex gap-2">
                {Object.entries(ROLE_PERMISSIONS).map(([role, config]) => (
                  <button
                    key={role}
                    onClick={() => setCurrentRole(role)}
                    className={`flex-1 px-3 py-2 rounded text-sm flex items-center justify-center gap-1 ${
                      currentRole === role ? 'ring-2 ring-white' : ''
                    }`}
                    style={{ backgroundColor: config.color }}
                  >
                    {role === 'viewer' && <Eye size={14} />}
                    {role === 'operator' && <Settings size={14} />}
                    {role === 'maintenance' && <Wrench size={14} />}
                    {role}
                  </button>
                ))}
              </div>
            </div>

            {/* Messages */}
            <div className="flex-1 bg-gray-900 rounded p-3 mb-4 overflow-y-auto max-h-96 space-y-2 font-mono text-sm">
              {messages.length === 0 && (
                <div className="text-gray-500 text-center py-4">
                  Try commands like:<br/>
                  "Start the pump"<br/>
                  "Set speed to 1500 RPM"<br/>
                  "Get status"<br/>
                  "Emergency stop"
                </div>
              )}
              {messages.map((msg, i) => (
                <div key={i} className={`${
                  msg.type === 'user' ? 'text-blue-400' :
                  msg.type === 'agent' ? 'text-purple-400' :
                  msg.type === 'success' ? 'text-green-400' :
                  msg.type === 'fail' ? 'text-red-400' :
                  msg.type === 'warning' ? 'text-yellow-400' :
                  msg.type === 'pass' ? 'text-green-300' :
                  msg.type === 'simulation' ? 'text-cyan-400' :
                  msg.type === 'critical' ? 'text-red-500 font-bold' :
                  msg.type === 'result' ? 'text-white bg-gray-800 p-2 rounded' :
                  'text-gray-300'
                }`}>
                  <span className="text-gray-500">[{msg.timestamp}]</span> {msg.text}
                </div>
              ))}
            </div>

            {/* Pending Approval */}
            {pendingApproval && (
              <div className="bg-yellow-900 rounded p-3 mb-4">
                <div className="font-bold mb-2">⏳ Pending Approval: {pendingApproval.operation}</div>
                <div className="text-sm mb-3">Task ID: {pendingApproval.taskId}</div>
                <div className="flex gap-2">
                  <button onClick={() => handleApproval(true)} className="flex-1 bg-green-600 hover:bg-green-700 rounded px-3 py-2 flex items-center justify-center gap-2">
                    <Check size={16} /> Approve
                  </button>
                  <button onClick={() => handleApproval(false)} className="flex-1 bg-red-600 hover:bg-red-700 rounded px-3 py-2 flex items-center justify-center gap-2">
                    <X size={16} /> Reject
                  </button>
                </div>
              </div>
            )}

            {/* Command Input */}
            <div className="flex gap-2">
              <input
                type="text"
                value={command}
                onChange={(e) => setCommand(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && command.trim()) {
                    processCommand(command);
                    setCommand('');
                  }
                }}
                placeholder="Enter natural language command..."
                className="flex-1 bg-gray-700 rounded px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
              <button
                onClick={() => {
                  if (command.trim()) {
                    processCommand(command);
                    setCommand('');
                  }
                }}
                className="bg-blue-600 hover:bg-blue-700 rounded px-4 py-2"
              >
                Send
              </button>
            </div>
          </div>

          {/* Right Panel: Safety Model & Audit */}
          <div className="space-y-4">
            {/* 5-Layer Safety Model */}
            <div className="bg-gray-800 rounded-lg p-4">
              <h2 className="text-xl font-bold mb-4 flex items-center gap-2">
                <Shield size={20} /> 5-Layer Safety Model
              </h2>
              <div className="space-y-2">
                {[
                  { layer: 1, name: 'RBAC', desc: 'Role-based access control', color: '#9b59b6' },
                  { layer: 2, name: 'Interlocks', desc: 'Predicate-based state guards', color: '#e74c3c' },
                  { layer: 3, name: 'Simulation', desc: 'Auto dry-run for HIGH risk', color: '#e67e22' },
                  { layer: 4, name: 'HITL', desc: 'Human approval for CRITICAL', color: '#f39c12' },
                  { layer: 5, name: 'Audit', desc: 'Hash-chained tamper-evident logs', color: '#27ae60' },
                ].map(({ layer, name, desc, color }) => (
                  <div key={layer} className="flex items-center gap-3 p-2 rounded" style={{ backgroundColor: `${color}33` }}>
                    <div className="w-8 h-8 rounded-full flex items-center justify-center text-sm font-bold" style={{ backgroundColor: color }}>
                      {layer}
                    </div>
                    <div>
                      <div className="font-bold">{name}</div>
                      <div className="text-xs text-gray-400">{desc}</div>
                    </div>
                  </div>
                ))}
              </div>
            </div>

            {/* Risk Levels */}
            <div className="bg-gray-800 rounded-lg p-4">
              <h3 className="font-bold mb-3">Risk Levels</h3>
              <div className="grid grid-cols-2 gap-2 text-sm">
                {Object.entries(OPERATIONS).map(([op, { risk, icon: Icon }]) => (
                  <div key={op} className="flex items-center gap-2 p-2 rounded bg-gray-700">
                    <Icon size={14} style={{ color: RISK_COLORS[risk] }} />
                    <span className="truncate">{op}</span>
                    <span className="ml-auto px-2 py-0.5 rounded text-xs" style={{ backgroundColor: RISK_COLORS[risk] }}>
                      {risk}
                    </span>
                  </div>
                ))}
              </div>
            </div>

            {/* Audit Log */}
            <div className="bg-gray-800 rounded-lg p-4">
              <h3 className="font-bold mb-3 flex items-center gap-2">
                <Lock size={16} /> Hash-Chained Audit Log
              </h3>
              <div className="max-h-40 overflow-y-auto space-y-1 text-xs font-mono">
                {auditLog.length === 0 ? (
                  <div className="text-gray-500">No audit entries yet...</div>
                ) : (
                  auditLog.slice(-10).reverse().map((entry, i) => (
                    <div key={i} className={`p-2 rounded ${
                      entry.result === 'success' ? 'bg-green-900/30' :
                      entry.result === 'denied' ? 'bg-red-900/30' :
                      'bg-yellow-900/30'
                    }`}>
                      <div className="flex justify-between">
                        <span className="text-gray-400">{entry.event}</span>
                        <span>{entry.tool}</span>
                      </div>
                      <div className="text-gray-500">hash: {entry.hash}...</div>
                    </div>
                  ))
                )}
              </div>
            </div>
          </div>
        </div>

        {/* Footer */}
        <div className="mt-6 text-center text-gray-500 text-sm">
          <p>TwinOps Demo | RWTH Aachen University — Chair of Information and Automation Systems</p>
          <p className="mt-1">Try different roles to see RBAC in action. Increase temperature above 95°C to trigger interlocks.</p>
        </div>
      </div>
    </div>
  );
}
