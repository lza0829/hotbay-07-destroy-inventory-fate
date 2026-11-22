import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Retention() {
  const [pols, setPols] = useState([])
  const [runs, setRuns] = useState([])
  const [policyId, setPolicyId] = useState('')
  const [msg, setMsg] = useState('')
  const reload = () => { api.policies().then(p => { setPols(p); if (p[0]) setPolicyId(String(p[0].id)) }); api.destroys().then(setRuns) }
  useEffect(() => { reload() }, [])
  async function run() {
    setMsg('')
    try {
      const r = await api.runRetention({ policy_id: Number(policyId) })
      const ghosts = r.affected.reduce((s, x) => s + (x.ghost_key_count || 0), 0)
      setMsg(`${r.policy} 影响 ${r.affected.length} 条 · 留幽灵占键 ${ghosts} 键（清单未清）`)
      reload()
    } catch (e) { setMsg(e.message) }
  }
  return (
    <div className="page">
      <h1>片龄保留</h1>
      <p className="lede">销毁唯一口径：终态已销毁、册面素材段拆除，湖对象目录占键保留为幽灵占键，回执如实标注清单未清。</p>
      <div className="panel form row">
        <label>策略<select value={policyId} onChange={e => setPolicyId(e.target.value)}>
          {pols.map(p => <option key={p.id} value={p.id}>{p.name}（{p.max_age_days}天 · {p.action}）</option>)}
        </select></label>
        <button type="button" onClick={run}>执行保留</button>
      </div>
      {msg && <p className="msg">{msg}</p>}
      <div className="table-wrap">
        <table>
          <thead><tr><th>流水</th><th>分区</th><th>策略</th><th>口径</th><th>状态</th><th>清单命运</th><th>幽灵键数</th><th>时间</th></tr></thead>
          <tbody>
            {runs.map(r => (
              <tr key={r.id}>
                <td>{r.id}</td><td>{r.part_key}</td><td>{r.policy_name}</td><td>{r.mode}</td>
                <td>{r.status === '已销毁' ? <span className="badge badge-destroyed">已销毁</span> : r.status}</td>
                <td>{r.manifest_cleared ? '已连清' : '清单未清（留幽灵）'}</td>
                <td>{r.ghost_key_count}</td><td>{r.created_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
