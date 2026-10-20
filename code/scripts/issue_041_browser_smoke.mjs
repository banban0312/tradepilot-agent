import { spawn } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const args = new Map()
for (let index = 2; index < process.argv.length; index += 2) {
  args.set(process.argv[index], process.argv[index + 1])
}

const browserPath = args.get('--browser')
const browserName = args.get('--name')
const version = args.get('--version')
const origin = process.env.TRADEPILOT_ISSUE041_BROWSER_ORIGIN
const password = process.env.TRADEPILOT_ISSUE041_BROWSER_PASSWORD
const reportRoot = args.get('--report-root')

if (!browserPath || !browserName || !version || !origin || !password || !reportRoot) {
  throw new Error('浏览器验收参数不完整。')
}

const viewports = [
  { width: 1366, height: 768 },
  { width: 1920, height: 1080 },
]
const wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds))

async function evaluate(send, expression) {
  const response = await send('Runtime.evaluate', {
    expression,
    returnByValue: true,
    awaitPromise: true,
  })
  if (response.exceptionDetails) {
    throw new Error(response.exceptionDetails.text || '页面脚本执行失败。')
  }
  return response.result?.value
}

async function waitFor(send, expression, description, timeoutMs = 15_000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const result = await evaluate(send, expression)
    if (result) return result
    await wait(200)
  }
  const diagnostics = await evaluate(
    send,
    '({url:location.href,readyState:document.readyState,title:document.title,text:document.body?.innerText?.slice(0,300)})',
  ).catch((error) => ({ diagnosticError: error.message }))
  throw new Error(`${browserName} ${version} 等待${description}超时；页面状态：${JSON.stringify(diagnostics)}`)
}

async function runViewport(viewport) {
  const profile = path.join(os.tmpdir(), `tp-issue041-${randomUUID()}`)
  mkdirSync(profile, { recursive: true })
  mkdirSync(reportRoot, { recursive: true })
  const browser = spawn(
    browserPath,
    [
      '--headless=new',
      '--disable-gpu',
      '--no-first-run',
      '--disable-background-networking',
      '--remote-debugging-port=0',
      '--remote-allow-origins=*',
      `--user-data-dir=${profile}`,
      `--window-size=${viewport.width},${viewport.height}`,
      `${origin}/login?redirect=/orders`,
    ],
    { stdio: 'ignore', windowsHide: true },
  )
  let socket
  try {
    const activePortFile = path.join(profile, 'DevToolsActivePort')
    const deadline = Date.now() + 20_000
    while (!existsSync(activePortFile) && Date.now() < deadline) {
      if (browser.exitCode !== null) throw new Error(`${browserName} ${version} 启动失败。`)
      await wait(100)
    }
    if (!existsSync(activePortFile)) throw new Error(`${browserName} ${version} DevTools 启动超时。`)
    const port = Number(readFileSync(activePortFile, 'utf8').split(/\r?\n/)[0])
    const targets = await fetch(`http://127.0.0.1:${port}/json/list`).then((response) => response.json())
    const target = targets.find((item) => item.type === 'page' && item.webSocketDebuggerUrl)
    if (!target) throw new Error(`${browserName} ${version} 未创建页面调试目标。`)

    socket = new WebSocket(target.webSocketDebuggerUrl)
    const pending = new Map()
    let sequence = 0
    socket.addEventListener('message', (event) => {
      const message = JSON.parse(event.data)
      if (!message.id) return
      const callbacks = pending.get(message.id)
      if (!callbacks) return
      pending.delete(message.id)
      if (message.error) callbacks.reject(new Error(message.error.message))
      else callbacks.resolve(message.result ?? {})
    })
    await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('DevTools WebSocket 连接超时。')), 10_000)
      socket.addEventListener('open', () => { clearTimeout(timeout); resolve() }, { once: true })
      socket.addEventListener('error', () => { clearTimeout(timeout); reject(new Error('DevTools WebSocket 连接失败。')) }, { once: true })
    })
    const send = (method, params = {}) => new Promise((resolve, reject) => {
      const id = ++sequence
      pending.set(id, { resolve, reject })
      socket.send(JSON.stringify({ id, method, params }))
    })

    await send('Page.enable')
    await send('Runtime.enable')
    await send('Emulation.setDeviceMetricsOverride', {
      width: viewport.width,
      height: viewport.height,
      deviceScaleFactor: 1,
      mobile: false,
    })
    await send('Page.navigate', { url: `${origin}/login?redirect=/orders` })
    await waitFor(send, "document.querySelector('#login-name') && document.querySelector('#login-password')", '登录页表单')
    const loginScript = `(() => {
      const setValue = (element, value) => {
        element.value = value;
        element.dispatchEvent(new Event('input', { bubbles: true }));
        element.dispatchEvent(new Event('change', { bubbles: true }));
      };
      setValue(document.querySelector('#login-name'), 'demo-operator-north');
      setValue(document.querySelector('#login-password'), ${JSON.stringify(password)});
      document.querySelector('#login-password').closest('form').requestSubmit();
      return true;
    })()`
    await evaluate(send, loginScript)
    await waitFor(
      send,
      "location.pathname === '/orders' && document.body.innerText.includes('订单中心')",
      '登录后的订单中心',
    )
    const browserWidth = await evaluate(send, 'window.innerWidth')
    const browserHeight = await evaluate(send, 'window.innerHeight')
    if (browserWidth !== viewport.width || browserHeight !== viewport.height) {
      throw new Error(`${browserName} ${version} 视口尺寸为 ${browserWidth}×${browserHeight}，预期 ${viewport.width}×${viewport.height}。`)
    }
    const prefix = `${browserName.toLowerCase()}-${version}-${viewport.width}x${viewport.height}`
    const ordersScreenshot = `${prefix}-orders.png`
    const ordersImage = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false })
    writeFileSync(path.join(reportRoot, ordersScreenshot), Buffer.from(ordersImage.data, 'base64'))
    const ordersLayout = await evaluate(send, `({title:document.querySelector('h1')?.innerText,scrollWidth:document.documentElement.scrollWidth,viewportWidth:window.innerWidth})`)
    if (!ordersLayout.title?.includes('订单中心') || ordersLayout.scrollWidth > ordersLayout.viewportWidth + 2) {
      throw new Error(`${browserName} ${version} 订单中心主区域标题或横向布局检查失败。`)
    }

    await send('Page.navigate', { url: `${origin}/` })
    await waitFor(send, "location.pathname === '/' && document.querySelector('#task-description')", '新任务工作台')
    const workbenchScreenshot = `${prefix}-workbench.png`
    const workbenchImage = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false })
    writeFileSync(path.join(reportRoot, workbenchScreenshot), Buffer.from(workbenchImage.data, 'base64'))
    const workbenchLayout = await evaluate(send, `({composer:document.body.innerText.includes('描述订单需求'),scrollWidth:document.documentElement.scrollWidth,viewportWidth:window.innerWidth})`)
    if (!workbenchLayout.composer || workbenchLayout.scrollWidth > workbenchLayout.viewportWidth + 2) {
      throw new Error(`${browserName} ${version} 新任务工作台主区域或横向布局检查失败。`)
    }
    return {
      width: viewport.width,
      height: viewport.height,
      login: 'passed',
      orders: 'passed',
      new_task_workbench: 'passed',
      document_horizontal_overflow: false,
      screenshots: [ordersScreenshot, workbenchScreenshot],
    }
  } finally {
    if (socket?.readyState === WebSocket.OPEN) socket.close()
    if (browser.exitCode === null) {
      browser.kill()
      await new Promise((resolve) => {
        if (browser.exitCode !== null) return resolve()
        browser.once('exit', resolve)
        setTimeout(resolve, 5_000).unref()
      })
    }
    const tempRoot = path.resolve(os.tmpdir())
    const profilePath = path.resolve(profile)
    if (profilePath.startsWith(`${tempRoot}${path.sep}`)) {
      rmSync(profilePath, { recursive: true, force: true })
    }
  }
}

const results = []
for (const viewport of viewports) results.push(await runViewport(viewport))
writeFileSync(
  path.join(reportRoot, `${browserName.toLowerCase()}-${version}-browser-smoke.json`),
  JSON.stringify({ browser: browserName, version, result: 'passed', viewports: results }, null, 2),
)
process.stdout.write(JSON.stringify({ browser: browserName, version, result: 'passed', viewports: results }))
