const fs = require('fs');
const path = require('path');
const { Document, Packer, Paragraph, TextRun, HeadingLevel, AlignmentType, LevelFormat, BorderStyle } = require('docx');

const outDir = __dirname;
const H1 = t => new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun(t)] });
const H2 = t => new Paragraph({ heading: HeadingLevel.HEADING_2, children: [new TextRun(t)] });
const P = (t, opts = {}) => new Paragraph({ spacing: { after: 130 }, children: [new TextRun({ text: t, ...opts })] });
const Code = t => new Paragraph({
  spacing: { after: 130 },
  shading: { fill: 'F2F2F2' },
  border: { left: { style: BorderStyle.SINGLE, size: 12, color: '888888', space: 4 } },
  children: [new TextRun({ text: t, font: 'Consolas', size: 20 })]
});
const Bullet = t => new Paragraph({ numbering: { reference: 'bullets', level: 0 }, children: [new TextRun(t)] });
const Num = t => new Paragraph({ numbering: { reference: 'numbers', level: 0 }, children: [new TextRun(t)] });

function build(name) {
  const steps = [
    ['打开 PowerShell', [
      P('点击电脑左下角的“开始”菜单，在搜索框输入 PowerShell，然后点击“Windows PowerShell”打开。不要打开 Git Bash。')
    ]],
    ['进入项目文件夹', [
      P('复制下面一行，粘贴到 PowerShell 窗口，然后按 Enter 回车：'),
      Code('cd "$env:USERPROFILE\\checkforCommercialhouse"'),
      P('如果没有出现红色错误提示，继续下一步。')
    ]],
    ['下载最新版本', [
      P('复制并执行下面一行：'),
      Code('git pull'),
      P('等待命令执行完成。出现“Fast-forward”或“Already up to date.”都表示这一步成功。')
    ]],
    ['重启系统服务', [
      P('继续复制并执行下面一行：'),
      Code('powershell -NoProfile -ExecutionPolicy Bypass -File .\\_restart_server.ps1'),
      P('等待几秒钟。如果看到“GET / -> 200”，说明服务已经启动成功。')
    ]],
    ['打开网页并刷新', [
      P('打开浏览器，在地址栏输入下面的网址并按回车：'),
      Code('http://127.0.0.1:8000'),
      P('如果网页之前已经打开，请按 Ctrl + F5 强制刷新，确保加载到最新版本。')
    ]],
    ['确认更新成功', [
      P('进入“核查记录”，每一条记录都应该有“查看对比图片”按钮。点击后可以查看房源照片、Google 街景照片、Smarty 验证图片、查重图片和其他核查截图。'),
      P('步骤1、步骤2、步骤3、步骤4以及备注现在可以点击标题展开或收起。待人工记录还会显示“人工复核”按钮。')
    ]]
  ];
  const children = [
    new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 180 }, children: [new TextRun({ text: `${name}电脑：系统更新操作说明`, bold: true, size: 34 })] }),
    new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 300 }, children: [new TextRun({ text: '请严格按照顺序操作，不需要懂技术', color: '666666' })] }),
    H1('一、更新前注意事项'),
    Bullet('如果当前正在运行房源核查任务，请先回到网页，点击“终止任务”，等任务结束后再更新。'),
    Bullet('不要删除项目文件夹。'),
    Bullet('不要执行 git reset、git clean 或其他自己不认识的命令。'),
    H1('二、按步骤更新'),
  ];
  for (const [title, body] of steps) { children.push(H2(title), ...body); }
  children.push(
    H1('三、如果 git pull 报网络错误'),
    P('只有普通的 git pull 出现网络连接错误时，才执行下面这行：'),
    Code('git -c http.proxy=http://127.0.0.1:52304 -c https.proxy=http://127.0.0.1:52304 pull'),
    P('执行成功后，继续执行“重启系统服务”那一行。普通 git pull 已经成功时，不要重复执行代理命令。'),
    H1('四、常见问题'),
    H2('1. 显示找不到路径'),
    P('说明没有进入正确的项目文件夹。重新执行：'),
    Code('cd "$env:USERPROFILE\\checkforCommercialhouse"'),
    H2('2. 显示 not a git repository'),
    P('说明当前目录不对。重新执行进入项目文件夹的命令，再执行 git pull。'),
    H2('3. 网页打不开'),
    P('重新执行重启服务命令，等待看到“GET / -> 200”，然后再次打开 http://127.0.0.1:8000。'),
    H2('4. 网页还是旧样式'),
    P('在网页中按 Ctrl + F5；如果仍未更新，关闭网页后重新打开 http://127.0.0.1:8000。'),
    H2('5. 提示本地文件有修改'),
    P('不要继续输入命令，也不要执行 git reset 或 git clean。请把 PowerShell 窗口里的完整提示截图发回。'),
    H1('五、最简操作顺序'),
    Num('进入项目文件夹：cd "$env:USERPROFILE\\checkforCommercialhouse"'),
    Num('下载更新：git pull'),
    Num('重启服务：powershell -NoProfile -ExecutionPolicy Bypass -File .\\_restart_server.ps1'),
    Num('打开网页：http://127.0.0.1:8000'),
    Num('按 Ctrl + F5 刷新页面。'),
    P('如果任何一步出现看不懂的红色错误，不要反复尝试，也不要删除文件，直接截图发回即可。', { bold: true })
  );
  const doc = new Document({
    numbering: { config: [
      { reference: 'bullets', levels: [{ level: 0, format: LevelFormat.BULLET, text: '•', alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 720, hanging: 360 } } } }] },
      { reference: 'numbers', levels: [{ level: 0, format: LevelFormat.DECIMAL, text: '%1.', alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 720, hanging: 360 } } } }] }
    ]},
    styles: { default: { document: { run: { font: 'Arial', size: 24 } } } },
    sections: [{ properties: { page: { size: { width: 12240, height: 15840 }, margin: { top: 1200, right: 1200, bottom: 1200, left: 1200 } } }, children }]
  });
  return Packer.toBuffer(doc).then(buf => fs.writeFileSync(path.join(outDir, `${name}_更新说明.docx`), buf));
}

Promise.all([build('LIU94'), build('pp')]);
