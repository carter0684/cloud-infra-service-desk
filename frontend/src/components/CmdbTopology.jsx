import React, { useMemo, useCallback } from 'react'
import {
  ReactFlow,
  Background,
  Controls,
  MarkerType,
  Position,
  Handle,
  BaseEdge,
  getBezierPath,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { COLORS, Badge } from '../utils/theme.js'

// ── Illumio-style network topology diagram (EC2 / RDS / Load Balancer) ────
// Renders the graph returned by GET /api/cmdb/{ec2,rds,lb}-topology: the
// target resource in the center, its VPC/subnet/security-group/target-
// group "will access" context nodes around it (yellow moving dots — the
// structural/policy relationship, not measured traffic), and the actual
// observed traffic peers: either real named peers (from VPC Flow Logs) —
// each one mandatorily classified as either the shared "User" node (👤,
// public or unresolved-private IPs) or a resolved CMDB "resource" node
// (a private IP that matched another known AWS resource) — or a single
// generic "Network" node (CloudWatch metrics fallback, when there's no
// usable flow log). Inbound-accepted traffic animates green regardless of
// whether its source is the User node or a resolved resource; outbound-
// accepted traffic animates red; rejected/no-activity edges are dashed
// grey with no animation.
//
// Built on @xyflow/react (the current/maintained package — the old
// `reactflow` npm name was renamed here in v12). Layout is a simple fixed
// radial arrangement computed client-side (context nodes above, traffic
// peers below) rather than an auto-layout library, since these graphs are
// always small, so a hand-computed layout is simpler and looks
// intentional rather than dumped through a generic force-directed
// algorithm.

const NODE_COLORS = {
  instance: COLORS.brand,
  vpc: '#64748b',
  subnet: '#64748b',
  security_group: '#7c3aed',
  target_group: '#7c3aed',
  target: '#0ea5e9',
  resource: COLORS.success,
  user: '#d97706',
  peer: COLORS.textSecondary,
  network: COLORS.textSecondary,
}

const NODE_ICON = {
  instance: '🖥️',
  vpc: '☁️',
  subnet: '🧩',
  security_group: '🛡️',
  target_group: '🎯',
  target: '📍',
  resource: '🔗',
  user: '👤',
  peer: '🌐',
  network: '🌐',
}

// resource_type -> a more specific icon than the generic "resource" one,
// used when a peer/target resolves to a known CMDB resource type.
const RESOURCE_TYPE_ICON = {
  'EC2 Instance': '🖥️',
  'RDS Instance': '🗄️',
  'Load Balancer': '⚖️',
}

function BaseNode({ data }) {
  const color = NODE_COLORS[data.kind] || COLORS.textMuted
  const icon = data.kind === 'resource' && data.resource_type
    ? (RESOURCE_TYPE_ICON[data.resource_type] || NODE_ICON.resource)
    : (NODE_ICON[data.kind] || '')
  return (
    <div
      title={data.tooltip || undefined}
      style={{
        border: `2px solid ${color}`,
        borderRadius: data.kind === 'instance' ? 10 : 8,
        background: data.kind === 'instance' ? color : '#fff',
        color: data.kind === 'instance' ? '#fff' : COLORS.textPrimary,
        padding: data.kind === 'instance' ? '10px 16px' : '7px 12px',
        fontSize: data.kind === 'instance' ? 13 : 11.5,
        fontWeight: data.kind === 'instance' ? 700 : 600,
        minWidth: data.kind === 'instance' ? 150 : 110,
        textAlign: 'center',
        boxShadow: data.kind === 'instance' ? '0 4px 14px rgba(26,64,128,0.35)' : '0 1px 4px rgba(15,23,42,0.08)',
        position: 'relative',
      }}
    >
      <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />
      <Handle type="source" position={Position.Bottom} style={{ opacity: 0 }} />
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} />
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />
      <div>{icon} {data.label}</div>
      {data.sublabel && (
        <div style={{ fontSize: 10, fontWeight: 400, opacity: 0.8, marginTop: 2 }}>{data.sublabel}</div>
      )}
    </div>
  )
}

const NODE_TYPES = { base: BaseNode }

// Custom animated edge: a moving colored dot travels along the bezier
// path (via SVG <animateMotion>), which is the actual Illumio-style
// visual the user asked for — "green dot move mean traffic in come, red
// dot move mean traffic out" — rather than xyflow's built-in dashed-line
// marching-ants animation. Direction of travel matches the edge's own
// source->target (inbound edges point peer->instance, outbound edges
// point instance->peer, so the dot always visually flows the right way).
function TrafficEdge({ id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, style, markerEnd, data }) {
  const [edgePath] = getBezierPath({ sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition })
  const dotColor = data?.dotColor
  const duration = data?.duration || '2.4s'
  return (
    <>
      <BaseEdge id={id} path={edgePath} style={style} markerEnd={markerEnd} />
      {dotColor && (
        <circle r="4.5" fill={dotColor}>
          <animateMotion dur={duration} repeatCount="indefinite" path={edgePath} />
        </circle>
      )}
    </>
  )
}

const EDGE_TYPES = { traffic: TrafficEdge }

// Fixed radial-ish layout: instance dead center, context nodes (vpc/
// subnet/sg) arranged in a ring above, traffic nodes (peer/network) in a
// ring below — mirrors Illumio's "policy context above, live traffic
// below" visual convention without needing a full auto-layout dependency.
function _placeRow(positions, nodes, y, spacing = 190) {
  const startX = 400 - ((nodes.length - 1) * spacing) / 2
  nodes.forEach((n, i) => { positions[n.id] = { x: startX + i * spacing, y } })
}

// Fixed radial-ish layout: instance dead center, "will access" context
// nodes (vpc/subnet/sg) in a ring above, the LB service chain
// (target_group -> target, when present) in two further rings above
// that (since they're a deeper level of the same "structural" hierarchy,
// not measured traffic), and observed-traffic nodes (resolved resource /
// User / generic peer / generic network) in a ring below — mirrors
// Illumio's "policy context above, live traffic below" visual
// convention without needing a full auto-layout dependency.
function computeLayout(nodes) {
  const instance = nodes.find(n => n.kind === 'instance')
  const context = nodes.filter(n => n.kind === 'vpc' || n.kind === 'subnet' || n.kind === 'security_group')
  const targetGroups = nodes.filter(n => n.kind === 'target_group')
  const targets = nodes.filter(n => n.kind === 'target')
  const traffic = nodes.filter(n => n.kind === 'peer' || n.kind === 'network' || n.kind === 'resource' || n.kind === 'user')

  const positions = {}
  if (instance) positions[instance.id] = { x: 400, y: 220 }

  _placeRow(positions, context, 60)
  _placeRow(positions, targetGroups, -60, 170)
  _placeRow(positions, targets, -180, 140)
  _placeRow(positions, traffic, 400, 170)

  return positions
}

// Yellow — the "will access" / structural-relationship color per the
// user's explicit request: network-context edges (instance↔SG, SG↔VPC/
// subnet, LB↔TargetGroup↔Target) get an animated yellow moving dot to
// show WHICH resources a policy allows reaching, distinct from the green/
// red used for actually-measured traffic below.
const CONTEXT_COLOR = '#eab308'

function edgeVisual(edge) {
  if (edge.kind === 'context') {
    return { stroke: CONTEXT_COLOR, strokeWidth: 1.5, dashed: true, animated: !!edge.animated }
  }
  // Traffic edge
  const isReject = edge.action === 'REJECT'
  if (isReject) {
    return { stroke: '#94a3b8', strokeWidth: 1.5, dashed: true, animated: false }
  }
  const isInbound = edge.direction === 'inbound'
  return {
    stroke: isInbound ? COLORS.success : COLORS.danger,
    strokeWidth: 2.5,
    dashed: false,
    animated: !!edge.animated,
  }
}

function formatBytes(n) {
  if (!n) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let v = n, i = 0
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(1)} ${units[i]}`
}

export default function CmdbTopology({ topology }) {
  const rawNodes = topology?.nodes || []
  const rawEdges = topology?.edges || []
  const positions = useMemo(() => computeLayout(rawNodes), [rawNodes])

  const flowNodes = useMemo(() => rawNodes.map(n => {
    let sublabel
    if (n.kind === 'instance') sublabel = `${n.private_ip || ''}${n.public_ip ? ' / ' + n.public_ip : ''}${n.endpoint ? n.endpoint : ''}${n.dns_name ? n.dns_name : ''}`
    else if (n.kind === 'security_group') sublabel = n.sg_id
    else if (n.kind === 'peer') sublabel = 'peer'
    else if (n.kind === 'resource') sublabel = n.resource_type || n.ip
    else if (n.kind === 'user') sublabel = (n.external_ips || []).length ? `${n.external_ips.length} external IP${n.external_ips.length === 1 ? '' : 's'}` : 'external'
    else if (n.kind === 'target_group') sublabel = `${n.protocol || ''}${n.port ? ':' + n.port : ''}`
    else if (n.kind === 'target') sublabel = n.health

    let tooltip
    if (n.kind === 'security_group') {
      const inb = (n.inbound_rules || []).join('\n') || '(none)'
      const outb = (n.outbound_rules || []).join('\n') || '(none)'
      tooltip = `Inbound:\n${inb}\n\nOutbound:\n${outb}`
    } else if (n.kind === 'user' && (n.external_ips || []).length) {
      tooltip = `Not resolved to a known CMDB resource:\n${n.external_ips.join('\n')}`
    }

    return {
      id: n.id,
      type: 'base',
      position: positions[n.id] || { x: 0, y: 0 },
      data: { ...n, sublabel, tooltip },
      draggable: true,
    }
  }), [rawNodes, positions])

  const flowEdges = useMemo(() => rawEdges.map(e => {
    const v = edgeVisual(e)
    let label
    if (e.kind === 'traffic') {
      const parts = []
      if (e.action) parts.push(e.action)
      if (e.dst_port) parts.push(`:${e.dst_port}`)
      if (e.bytes) parts.push(formatBytes(e.bytes))
      label = parts.join(' ')
    }
    // Context ("will access") edges get a slower-moving yellow dot than
    // traffic edges — they represent a static structural relationship,
    // not a live flow, so a gentler pace reads as "always available"
    // rather than "actively streaming right now".
    const isMovingDot = v.animated && (e.kind === 'traffic' || e.kind === 'context')
    return {
      id: e.id,
      source: e.source,
      target: e.target,
      type: isMovingDot ? 'traffic' : undefined,
      data: isMovingDot ? { dotColor: v.stroke, duration: e.kind === 'context' ? '3.6s' : '2.4s' } : undefined,
      label,
      labelStyle: { fontSize: 10, fill: v.stroke, fontWeight: 600 },
      labelBgStyle: { fill: '#fff', fillOpacity: 0.85 },
      style: { stroke: v.stroke, strokeWidth: v.strokeWidth, strokeDasharray: v.dashed ? '6 4' : undefined },
      markerEnd: e.kind === 'traffic' ? { type: MarkerType.ArrowClosed, color: v.stroke, width: 16, height: 16 } : undefined,
    }
  }), [rawEdges])

  const onInit = useCallback((instance) => instance.fitView({ padding: 0.25 }), [])

  return (
    <div style={{ width: '100%', height: 480, position: 'relative', background: '#f8fafc', borderRadius: 10, border: `1px solid ${COLORS.border}` }}>
      <div style={{ position: 'absolute', top: 10, left: 12, zIndex: 5, display: 'flex', gap: 8, alignItems: 'center' }}>
        {topology?.has_flow_logs ? (
          <Badge text="LIVE — VPC Flow Logs" color={COLORS.success} />
        ) : (
          <Badge text="FALLBACK — CloudWatch metrics only" color={COLORS.warning} />
        )}
        {topology?.flow_log_truncated && <Badge text="Peer list truncated" color={COLORS.warning} />}
        {topology?.is_mock && <Badge text="MOCK DATA (local dev)" color={COLORS.textMuted} />}
      </div>
      <div style={{ position: 'absolute', bottom: 10, left: 12, zIndex: 5, display: 'flex', gap: 14, flexWrap: 'wrap', fontSize: 11, color: COLORS.textSecondary, background: 'rgba(255,255,255,0.9)', padding: '5px 10px', borderRadius: 6, maxWidth: '95%' }}>
        <span><span style={{ display: 'inline-block', width: 10, height: 10, borderRadius: '50%', background: COLORS.success, marginRight: 5 }} />Inbound traffic</span>
        <span><span style={{ display: 'inline-block', width: 10, height: 10, borderRadius: '50%', background: COLORS.danger, marginRight: 5 }} />Outbound traffic</span>
        <span><span style={{ display: 'inline-block', width: 10, height: 10, borderRadius: '50%', background: CONTEXT_COLOR, marginRight: 5 }} />Will access (network structure)</span>
        <span><span style={{ display: 'inline-block', width: 10, height: 2, background: '#94a3b8', marginRight: 5, verticalAlign: 'middle' }} />Rejected / no activity</span>
        <span>👤 User (unresolved/public traffic)</span>
        <span>🔗 Resolved CMDB resource</span>
      </div>
      <ReactFlow
        nodes={flowNodes}
        edges={flowEdges}
        nodeTypes={NODE_TYPES}
        edgeTypes={EDGE_TYPES}
        onInit={onInit}
        proOptions={{ hideAttribution: true }}
        minZoom={0.3}
        maxZoom={2}
      >
        <Background color="#e2e8f0" gap={20} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  )
}
