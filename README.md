# C_Drive_Cleaner 🧹✨

[English](#english) | [中文](#中文)

---

<a name="english"></a>
## 📖 English

**A simple C-drive space visualizer built during my first "Vibe Coding" attempt.**

To be honest, as a programming beginner, I just learned about Vibe Coding and had no idea what to build. So I decided to make a useful little tool to help me clean my own C-drive (which is always running out of space). 

It's a pure Python + HTML tool. **It only scans, absolutely no files are deleted automatically!**

### ✨ Features
* **Visualization**: Beautiful capsule cards and progress bars instead of boring data tables.
* **Infinite Drill-down**: Double-click folders to drill down layer by layer, until you find the biggest space eater.
* **Path Memory**: A tree-style history to quickly jump back to directories you just explored.
* **Safety First**: 100% read-only. Code contains no `os.remove` or `shutil.rmtree` logic. Safe for system folders.

### 🎨 UI Design
* **Glassmorphism**: Frosted glass effect for a modern, sleek look.
* **Macaron Colors**: Soft pastel colors that are gentle on the eyes.
* **Micro-interactions**: Ripple effects on buttons and subtle floating animations.

### 🚀 Quick Start
1. Make sure you have Python 3 installed.
2. Install dependency: `pip install flask`
3. Double-click `start.bat`, and the web page will open automatically.
4. Click "Start Scan" in the browser.

### 📄 License
This project is licensed under the MIT License.

---

<a name="中文"></a>
## 📖 中文

**这是一个我在第一次尝试 Vibe Coding 时做的 C 盘空间可视化小工具。**

说实话，作为一个编程初学者，刚接触 Vibe Coding，不知道做点什么好，就顺手写了一个能帮我清理自己 C 盘（它总是爆红）的实用小工具。

这是一个纯 Python + HTML 的工具。**它只扫描，绝对不会自动删除任何文件！**

### ✨ 核心功能
* **可视化空间占用**：胶囊卡片和进度条排版，拒绝冰冷的数据表格。
* **无限层级下钻**：双击文件夹一层层深挖，直到抓出占用最大的那个“大老鼠”。
* **路径记忆树**：树状记录浏览历史，一键跳转，不用反复点“返回”。
* **绝对安全**：纯读取。代码内部没有任何删除文件的逻辑，扫描系统关键文件夹也不会报错中断。

### 🎨 UI 设计
* **液态玻璃（毛玻璃）**：极致通透的 UI 质感，看起来非常舒适。
* **马卡龙配色**：柔和的低饱和度色彩，不刺眼。
* **微交互**：按钮点击涟漪效果、卡片悬浮呼吸感。

### 🚀 快速开始
1. 确保安装了 Python 3。
2. 安装依赖：`pip install flask`
3. 双击 `start.bat`，网页会自动打开。
4. 在网页中点击“开始扫描”即可。

### 📄 开源协议
本项目基于 MIT License 协议开源。
