// 训练中摄像头看动作（spec [2026-10-23] §218 深蹲、§219 再加六类、§220 和 §221 再扩）。
// 识别在这台设备上跑（MediaPipe 姿态识别，文件由 IronM 自己的服务器提供），画面不上传、不保存。
// 判断全由下面的规则表和代码算，不用大模型；语音用设备自带的朗读。
// 分两层：analyser 只吃关节点、吐事件（能在 node 里测）；startCoach 管摄像头、画火柴人、说话。

// ---- 哪些动作能看：动作名 → 类 ---------------------------------------------------------------
export const MOVES = {
  自重深蹲: "squat", 高脚杯深蹲: "squat", 箱式深蹲: "squat", 杠铃深蹲: "squat",
  俯卧撑: "pushup", 窄距俯卧撑: "pushup", 上斜俯卧撑: "pushup", 下斜俯卧撑: "pushup",
  罗马尼亚硬拉: "rdl", 杠铃硬拉: "deadlift",
  后撤弓步: "split", 原地分腿蹲: "split", 保加利亚分腿蹲: "split",
  臀桥: "bridge", 哑铃臀推: "bridge", 单腿臀桥: "bridge",
  平板支撑: "plank",
  哑铃推举: "press", 杠铃站姿推举: "press",
  侧平举: "raise", 哑铃前平举: "raise",
  // §220：照现有规则再扩 12 个
  杠铃前蹲: "squat", 相扑深蹲: "squat", 深蹲跳: "jumpsquat", 跪姿俯卧撑: "kneepush",
  六角杠硬拉: "deadlift", 架上硬拉: "deadlift", 壶铃相扑硬拉: "deadlift", 杠铃早安式: "goodmorning",
  侧平板支撑: "sideplank", 阿诺德推举: "press", 拇指朝上斜前举: "raise", 哑铃耸肩: "shrugs",
  // §221：第二批，再加九类 18 个
  哑铃弯举: "curl", 锤式弯举: "curl", 杠铃弯举: "curl", 佐特曼弯举: "curl", 杠铃反握弯举: "curl",
  凳上臂屈伸: "dip", 双杠臂屈伸: "dip", 哑铃颈后臂屈伸: "overhead",
  引体向上: "pullup", 反手引体向上: "pullup", 器械辅助引体向上: "pullup", 反向划船: "row",
  开合跳: "jacks", 星形跳: "jacks", 提踵: "calf", 哥萨克深蹲: "lateral", 哑铃侧弓步: "lateral", 仰卧举腿: "legraise",
  // §222：第三批，再加九类 20 个
  哑铃卧推: "bench", 杠铃卧推: "bench", 上斜哑铃卧推: "bench", 上斜杠铃卧推: "bench", 下斜杠铃卧推: "bench",
  窄距杠铃卧推: "bench", 哑铃地板卧推: "bench", 哑铃对握卧推: "bench",
  单臂哑铃划船: "bentrow", 杠铃俯身划船: "bentrow", "T 杠划船": "bentrow", 俯卧上斜哑铃划船: "bentrow", 杠铃彭德雷划船: "bentrow",
  壶铃摇摆: "kb", 单腿罗马尼亚硬拉: "slrdl", 悬垂举腿: "hangraise", 哑铃直立划船: "upright",
  俯身哑铃反向飞鸟: "fly", 哑铃俯身臂屈伸: "kickback", 仰卧曲杆臂屈伸: "skull",
};

// ---- 规则表：量什么、什么时候量、多少算错、对应提醒、出处 ---------------------------------------
// cue 是练的时候说的口令（不超过 5 个字）；short 用在一组结束的总结里；rank 越小越先说（会伤人的先说）。
// 出处：依据库编号，或动作库里这个动作写的要点、常见错误；具体阈值依据里没有的写〔经验〕（§219 需求 3）。
const LIB = (move, what) => `动作库「${move}」的${what}`;
export const RULES = [
  // 深蹲（§218）
  { id: "valgus", kinds: ["squat", "jumpsquat"], view: "front", name: "膝盖内扣", cue: "膝盖往外", short: "膝盖内扣", rank: 1,
    measure: "两膝间距和两踝间距之比", when: "下蹲过程中，连续 4 帧", limit: 0.95, bad: "膝距小于踝距的 95%",
    why: "臀部外侧没发力或重量太重", source: "K25（膝盖与脚尖方向一致）", threshold: "〔经验〕" },
  { id: "depth", kinds: ["squat"], view: "side", name: "蹲得不够深", cue: "再蹲低点", short: "没蹲够", rank: 2,
    measure: "最低点时髋比膝高多少（按腿长算）", when: "每一下的最低点", limit: 0.06, bad: "髋比膝高出腿长的 6% 以上",
    why: "脚踝或髋活动度不够，或者重量太重", source: "K25（蹲到大腿约与地面平行）", threshold: "〔经验〕：髋点是关节中心，留了余量" },
  { id: "lean", kinds: ["squat"], view: "side", name: "上身前倾过多", cue: "胸口抬起", short: "前倾多", rank: 3,
    measure: "肩—髋连线和竖直方向的夹角", when: "每一下的最低点", limit: 50, bad: "超过 50°",
    why: "核心没撑住、重心靠前或重量太重", source: "K25（背部平直、躯干稳定）", threshold: "〔经验〕" },
  { id: "heel", kinds: ["squat"], view: "side", name: "脚跟离地", cue: "脚跟踩实", short: "脚跟起来", rank: 4,
    measure: "脚跟比站着时抬高多少（按站着时的腿长算）", when: "下蹲过程中，膝角大于 70° 且脚跟脚尖都看得清时，连续 4 帧", limit: 0.08, bad: "抬高腿长的 8% 以上",
    why: "脚踝活动度不够或重心太靠前", source: "K25（脚跟不离地）",
    threshold: "〔经验〕；11-16 按实测加了看得清的条件：蹲得很深时脚被臀部挡住，脚跟点会乱漂" },
  // 俯卧撑、平板支撑：身体一条直线（§219）
  { id: "sag", kinds: ["pushup", "kneepush", "plank", "row"], view: "side", name: "塌腰", cue: "收紧腰腹", short: "塌腰", rank: 1,
    measure: "肩—髋—踝（跪姿量到膝）的夹角离 180° 差多少，髋在连线下面", when: "撑着的过程中，连续 4 帧", limit: 15, bad: "差 15° 以上",
    why: "腹部和臀部没收紧，或者力气用完了", source: LIB("俯卧撑", "常见错误「塌腰或撅臀」、平板支撑的要点「不塌腰、不撅臀」、反向划船的常见错误「屁股下垂」"), threshold: "〔经验〕" },
  { id: "pike", kinds: ["pushup", "kneepush", "plank"], view: "side", name: "撅臀", cue: "屁股放低", short: "撅臀", rank: 2,
    measure: "肩—髋—踝的夹角离 180° 差多少，髋在肩踝连线上面", when: "撑着的过程中，连续 4 帧", limit: 15, bad: "差 15° 以上",
    why: "想省力，或者核心撑不住", source: LIB("俯卧撑", "常见错误「塌腰或撅臀」、平板支撑的要点「不塌腰、不撅臀」"), threshold: "〔经验〕" },
  { id: "shallowPush", kinds: ["pushup", "kneepush"], view: "side", name: "下得不够深", cue: "胸口再低", short: "没下到位", rank: 3,
    measure: "最低点时肘的角度", when: "每一下的最低点", limit: 100, bad: "肘没弯到 100° 以内",
    why: "力量不够或者在赶次数", source: LIB("俯卧撑", "要点「下放时控制速度」"), threshold: "〔经验〕" },
  // 硬拉（§219）
  { id: "bar", kinds: ["rdl", "deadlift"], view: "side", name: "杠离身体远", cue: "杠贴着腿", short: "杠离身体远", rank: 1,
    measure: "手腕（代替杠）离脚踝的前后距离（按腿长算）", when: "髋折下去的过程中，连续 4 帧", limit: 0.18, bad: "超过腿长的 18%",
    why: "背阔肌没收紧，杠往前飘，腰的负担变大", source: "K26（杠铃贴近小腿、贴着腿上升）", threshold: "〔经验〕：手腕代替杠、脚踝代替脚掌中部，留了余量" },
  { id: "squatty", kinds: ["rdl", "goodmorning", "kb"], view: "side", name: "膝盖弯太多", cue: "屁股往后推", short: "膝盖弯太多", rank: 2,
    measure: "最低点时膝的角度", when: "每一下的最低点", limit: 125, bad: "膝弯到 125° 以内，变成深蹲",
    why: "髋往后折得不够，用蹲代替了折", source: LIB("罗马尼亚硬拉", "常见错误「膝盖弯得太多，变成了深蹲」、壶铃摇摆的常见错误「做成了深蹲」"), threshold: "〔经验〕" },
  // 分腿蹲、弓步（§219）
  { id: "splitDepth", kinds: ["split"], view: "side", name: "蹲得不够深", cue: "再往下点", short: "没蹲够", rank: 1,
    measure: "最低点时前腿的髋比膝高多少（按腿长算）", when: "每一下的最低点", limit: 0.15, bad: "高出腿长的 15% 以上",
    why: "步子太小或怕膝盖疼", source: LIB("后撤弓步", "常见错误「步子太小，前膝压力大」"), threshold: "〔经验〕" },
  { id: "splitLean", kinds: ["split"], view: "side", name: "上身前倾过多", cue: "上身立直", short: "前倾多", rank: 2,
    measure: "肩—髋连线和竖直方向的夹角", when: "每一下的最低点", limit: 35, bad: "超过 35°",
    why: "核心没撑住，或者重心往前扑", source: LIB("后撤弓步", "常见错误「身体向前倒」"), threshold: "〔经验〕：保加利亚分腿蹲本来允许小幅前倾，放宽到 35°" },
  // 臀桥（§219）
  { id: "bridgeTop", kinds: ["bridge"], view: "side", name: "没顶到位", cue: "臀部顶高", short: "没顶到位", rank: 1,
    measure: "顶到最高时肩—髋—膝的夹角", when: "每一下的最高点", limit: 170, bad: "不到 170°",
    why: "臀部没发力，或者重量太重", source: LIB("臀桥", "要点「用臀部发力，不用腰去顶」"), threshold: "〔经验〕" },
  // 推举（§219）
  { id: "lockout", kinds: ["press"], view: "side", name: "没推到顶", cue: "手臂推直", short: "没推直", rank: 2,
    measure: "推到最高时肘的角度", when: "每一下的最高点", limit: 160, bad: "不到 160°",
    why: "重量太重或肩的活动度不够", source: LIB("哑铃推举", "要点"), threshold: "〔经验〕" },
  { id: "arch", kinds: ["press", "overhead"], view: "side", name: "腰往后仰", cue: "收紧腹部", short: "腰后仰", rank: 1,
    measure: "肩比髋往后偏，肩—髋连线和竖直方向的夹角", when: "推起过程中，连续 4 帧", limit: 15, bad: "往后仰超过 15°",
    why: "重量太重，用腰代偿", source: LIB("哑铃推举", "常见错误「腰部反弓借力」、哑铃颈后臂屈伸的常见错误「腰往后弯」"), threshold: "〔经验〕" },
  // 平举（§219）
  { id: "shrug", kinds: ["raise"], view: "front", name: "耸肩", cue: "肩膀放下", short: "耸肩", rank: 1,
    measure: "耳朵到肩的高度，和开始时放松站着比", when: "抬起过程中，连续 4 帧", limit: 0.8, bad: "缩到开始时的 80% 以下",
    why: "重量太重，斜方肌抢着发力", source: LIB("侧平举", "要点「不耸肩」、常见错误「抬得太高、耸肩」"), threshold: "〔经验〕" },
  // §220
  { id: "hipDrop", kinds: ["sideplank"], view: "any", name: "髋部下垂", cue: "髋往上顶", short: "髋往下塌", rank: 1,
    measure: "肩—髋—踝的夹角离 180° 差多少，髋在连线下面（靠地那边）", when: "撑着的过程中，连续 4 帧", limit: 15, bad: "差 15° 以上",
    why: "侧腰和臀部没撑住，或者力气用完了", source: LIB("侧平板支撑", "要点「髋部不下沉」、常见错误「髋部下垂」"), threshold: "〔经验〕" },
  { id: "shrugLow", kinds: ["shrugs"], view: "front", name: "没耸到位", cue: "再耸高点", short: "没耸到位", rank: 1,
    measure: "耸到最高时耳朵到肩的高度，和开始时放松站着比", when: "每一下的最高点", limit: 0.75, bad: "还剩开始时的 75% 以上",
    why: "重量太重或者在赶次数", source: LIB("哑铃耸肩", "要点「直上直下」"), threshold: "〔经验〕" },  // §221 第二批
  { id: "swing", kinds: ["curl"], view: "side", name: "身体借力", cue: "身体别晃", short: "身体晃", rank: 1,
    measure: "肩—髋连线前后摆动的幅度", when: "这一下全程", limit: 12, bad: "前后摆动超过 12°",
    why: "重量太重，用身体甩起来", source: LIB("哑铃弯举", "常见错误「身体后仰借力」、锤式弯举等的常见错误「身体前后晃借力」"), threshold: "〔经验〕" },
  { id: "elbowDrift", kinds: ["curl"], view: "side", name: "大臂往前跑", cue: "大臂夹紧", short: "大臂前移", rank: 2,
    measure: "肩—肘连线和竖直方向的夹角", when: "弯起过程中，连续 4 帧", limit: 30, bad: "超过 30°",
    why: "肩膀前侧帮着抬，或者重量太重", source: LIB("哑铃弯举", "常见错误「手肘向前移」、要点「上臂不前后移动」"), threshold: "〔经验〕：弯到顶时大臂本来会往前一点，留了余量" },
  { id: "dipDeep", kinds: ["dip"], view: "side", name: "下得太深", cue: "别下太深", short: "下太深", rank: 1,
    measure: "肩比肘低多少（按上臂长算）", when: "下沉过程中，连续 4 帧", limit: 0.15, bad: "低出上臂长的 15% 以上（上臂往下斜约 9° 以上）",
    why: "想多下一点，肩膀前侧被拉得太开", source: LIB("凳上臂屈伸", "常见错误「下得太深，肩膀前侧压力大」、双杠臂屈伸的常见错误「下沉太深」，步骤「到上臂接近水平」"), threshold: "〔经验〕" },
  { id: "armFwd", kinds: ["overhead"], view: "side", name: "大臂没竖直", cue: "大臂贴耳", short: "大臂前倒", rank: 2,
    measure: "肩—肘连线和竖直方向的夹角", when: "放下过程中，连续 4 帧", limit: 30, bad: "超过 30°",
    why: "肩膀活动度不够，或者重量太重", source: LIB("哑铃颈后臂屈伸", "要点「上臂贴近耳朵」"), threshold: "〔经验〕" },
  { id: "chinUp", kinds: ["pullup"], view: "front", name: "只拉一半", cue: "下巴过杠", short: "没拉过杠", rank: 1,
    measure: "拉到最高时嘴和两手的高低", when: "每一下的最高点", limit: 0, bad: "嘴没高过两手",
    why: "力量不够或者在赶次数", source: LIB("引体向上", "步骤「到下巴过杠」、常见错误「只做一半幅度」"), threshold: "〔经验〕：嘴代替下巴、手腕代替杠" },
  { id: "hangLow", kinds: ["pullup"], view: "front", name: "没放到底", cue: "手臂伸直", short: "没放直", rank: 2,
    measure: "拉这一下之前放下来时两肘的角度", when: "每一下开始拉之前", limit: 150, bad: "肘没伸到 150° 就开始拉",
    why: "力量不够或者在赶次数", source: LIB("引体向上", "步骤「慢慢下放到手臂伸直」、反手引体向上的常见错误「只拉一半」"), threshold: "〔经验〕" },
  { id: "rowTop", kinds: ["row"], view: "side", name: "没拉到位", cue: "胸口贴杠", short: "没拉到位", rank: 2,
    measure: "拉到最高时肘的角度", when: "每一下的最高点", limit: 90, bad: "肘没弯到 90° 以内",
    why: "背没发力，只用手臂拉", source: LIB("反向划船", "要点「胸口去碰杠」、常见错误「只用手臂拉」"), threshold: "〔经验〕" },
  { id: "feetOut", kinds: ["jacks"], view: "front", name: "脚没跳开", cue: "脚再分开", short: "脚没跳开", rank: 1,
    measure: "跳开时两踝间距和肩宽之比", when: "这一下里最大的", limit: 1.3, bad: "不到肩宽的 1.3 倍",
    why: "累了或者节奏太快", source: LIB("开合跳", "步骤「双脚向两侧分开落地」"), threshold: "〔经验〕" },
  { id: "armsUp", kinds: ["jacks"], view: "front", name: "手没举过头", cue: "手举过头", short: "手没举高", rank: 2,
    measure: "手腕比肩高多少（按手臂长算）", when: "这一下里最高的", limit: 0.6, bad: "不到手臂长的 60%",
    why: "累了或者节奏太快", source: LIB("开合跳", "步骤「双手从两侧举过头顶」、星形跳的步骤「双臂向两侧斜上方打开」"), threshold: "〔经验〕：斜上方 45° 约是 70%" },
  { id: "calfLow", kinds: ["calf"], view: "side", name: "幅度太小", cue: "再踮高点", short: "幅度小", rank: 1,
    measure: "踮到最高时脚跟比开始站着时抬高多少（按腿长算）", when: "每一下的最高点", limit: 0.05, bad: "不到腿长的 5%",
    why: "在赶次数或者重量太重", source: LIB("提踵", "常见错误「幅度太小」、步骤「踮起脚尖到最高处」"), threshold: "〔经验〕" },
  { id: "sideKnee", kinds: ["lateral"], view: "front", name: "膝盖内扣", cue: "膝盖往外", short: "膝盖内扣", rank: 1,
    measure: "屈的那条腿膝盖比脚踝往身体中间偏多少（按小腿长算）", when: "每一下的最低点", limit: 0.15, bad: "偏出小腿长的 15% 以上",
    why: "臀部外侧没发力或重量太重", source: LIB("哑铃侧弓步", "常见错误「膝盖向内扣」、要点「屈的那侧膝盖顺着脚尖」"), threshold: "〔经验〕" },
  { id: "sideLean", kinds: ["lateral"], view: "front", name: "上身前倾过多", cue: "胸口抬起", short: "前倾多", rank: 2,
    measure: "最低点时躯干在画面里的高度，和开始站直时比（前倾越多越短）", when: "每一下的最低点", limit: 0.75, bad: "缩到 75% 以下（约前倾 40° 以上）",
    why: "髋和大腿内侧活动度不够，或者核心没撑住", source: LIB("哥萨克深蹲", "常见错误「上身过度前倾弯腰」、哑铃侧弓步的常见错误「上身过度前倾」"), threshold: "〔经验〕" },
  { id: "legLow", kinds: ["legraise"], view: "side", name: "腿没举够高", cue: "腿再举高", short: "腿没举高", rank: 1,
    measure: "举到最高时肩—髋—踝的夹角", when: "每一下的最高点", limit: 110, bad: "没到 110° 以内（腿和地面不到约 70°）",
    why: "腹部力量不够或者在赶次数", source: LIB("仰卧举腿", "步骤「慢慢抬到与地面垂直」"), threshold: "〔经验〕" },
  { id: "legBend", kinds: ["legraise"], view: "side", name: "腿弯了", cue: "腿伸直", short: "腿弯了", rank: 2,
    measure: "髋—膝—踝的夹角", when: "举腿过程中，连续 4 帧", limit: 140, bad: "弯到 140° 以内",
    why: "腹部力量不够，弯腿省力", source: LIB("仰卧举腿", "步骤「双腿伸直并拢」"), threshold: "〔经验〕" },
  // §222 第三批
  { id: "dropFast", kinds: ["bench", "skull"], view: "side", name: "下放太快", cue: "慢慢放下", short: "放太快", rank: 1,
    measure: "肘过 120° 到最低点用了多久", when: "每一下往下放的时候", limit: 0.3, bad: "不到 0.3 秒",
    why: "想借反弹的劲推起来", source: LIB("哑铃卧推", "常见错误「下放太快、在底部反弹」、杠铃卧推的「杠铃在胸口反弹」、仰卧曲杆臂屈伸的「下放太快」"), threshold: "〔经验〕：控制着放一般要 0.5 秒以上" },
  { id: "hipUp", kinds: ["bench"], view: "side", name: "屁股离开凳子", cue: "屁股贴凳", short: "屁股抬起", rank: 2,
    measure: "髋比开始前躺好时抬高多少（按躯干长算）", when: "这一下全程，连续 4 帧", limit: 0.1, bad: "抬高躯干长的 10% 以上",
    why: "重量太重，用腿蹬、腰拱起来借力", source: LIB("杠铃卧推", "常见错误「臀部离开凳子」，上斜杠铃卧推、窄距杠铃卧推同"), threshold: "〔经验〕" },
  { id: "torsoRise", kinds: ["bentrow", "fly"], view: "side", name: "上身起伏借力", cue: "上身别动", short: "上身起伏", rank: 1,
    measure: "肩—髋连线和竖直方向的夹角在这一下里变了多少", when: "这一下全程", limit: 15, bad: "变化超过 15°",
    why: "重量太重，用上身甩", source: LIB("杠铃俯身划船", "常见错误「起身借力把杠铃甩上来」，T 杠划船「靠上身起伏借力」、彭德雷划船「上身越抬越高」、单臂哑铃划船「扭身体借力」、俯卧上斜哑铃划船「胸口离开靠背借力」、俯身哑铃反向飞鸟「身体上下起伏」"), threshold: "〔经验〕" },
  { id: "rowShort", kinds: ["bentrow"], view: "side", name: "没拉到位", cue: "再往后拉", short: "没拉到位", rank: 2,
    measure: "拉到最高时肘的角度", when: "每一下的最高点", limit: 100, bad: "肘没弯到 100° 以内",
    why: "重量太重或者在赶次数", source: LIB("杠铃俯身划船", "步骤「把杠铃拉向肚脐」「顶端停一秒」，单臂哑铃划船「屈肘把哑铃拉向髋部」"), threshold: "〔经验〕" },
  { id: "kbArms", kinds: ["kb"], view: "side", name: "用手臂抬", cue: "髋往前顶", short: "手臂抬", rank: 2,
    measure: "壶铃荡到胸口高度时髋的角度", when: "每一下壶铃到胸口时", limit: 160, bad: "髋还没站直（不到 160°）",
    why: "髋没发力，用手臂和肩膀把壶铃抬上去", source: LIB("壶铃摇摆", "常见错误「用手臂往上抬」、要点「发力来自髋部前推，不是用手臂抬」"), threshold: "〔经验〕" },
  { id: "hipOpen", kinds: ["slrdl"], view: "side", name: "髋往外翻", cue: "髋放平", short: "髋外翻", rank: 1,
    measure: "最低点时左右两髋上下错开多少（按躯干长算）", when: "每一下的最低点", limit: 0.15, bad: "错开躯干长的 15% 以上",
    why: "后腿那侧的髋跟着抬，身体转开了", source: LIB("单腿罗马尼亚硬拉", "常见错误「后腿那侧的髋部向外翻」、要点「髋部保持水平」"), threshold: "〔经验〕：侧面拍时远处那侧的髋常被挡着，要用真视频确认" },
  { id: "seesaw", kinds: ["slrdl"], view: "side", name: "后腿没抬起来", cue: "后腿抬高", short: "后腿没抬", rank: 2,
    measure: "最低点时肩—髋—后脚踝的夹角", when: "每一下的最低点", limit: 150, bad: "不到 150°（后腿没和上身连成一条线）",
    why: "只是弯腰，没有用髋折叠", source: LIB("单腿罗马尼亚硬拉", "步骤「上身前倾，后腿向后抬起，身体像跷跷板」、常见错误「弯腰去够地面」"), threshold: "〔经验〕" },
  { id: "hangHalf", kinds: ["hangraise"], view: "side", name: "只抬到一半", cue: "腿再抬高", short: "没抬到位", rank: 1,
    measure: "抬到最高时肩—髋—膝的夹角", when: "每一下的最高点", limit: 100, bad: "没到 100° 以内（大腿没到和地面差不多平）",
    why: "腹部力量不够或者在赶次数", source: LIB("悬垂举腿", "常见错误「只抬到一半就放下」、步骤「把腿抬到和地面平行或更高」"), threshold: "〔经验〕" },
  { id: "hangSwing", kinds: ["hangraise"], view: "side", name: "身体前后荡", cue: "身体别荡", short: "身体荡", rank: 2,
    measure: "肩—髋连线和竖直方向的夹角在这一下里变了多少", when: "这一下全程", limit: 25, bad: "变化超过 25°",
    why: "靠荡的劲把腿甩上去", source: LIB("悬垂举腿", "常见错误「靠身体荡起来甩腿」"), threshold: "〔经验〕：抬腿时骨盆往上卷，上身本来会往后倒一点" },
  { id: "elbowHigh", kinds: ["upright"], view: "front", name: "手肘高过肩", cue: "肘别过肩", short: "肘过肩", rank: 1,
    measure: "肘比肩高多少（按上臂长算）", when: "拉起过程中，连续 4 帧", limit: 0.1, bad: "高出上臂长的 10% 以上",
    why: "拉得太高，肩膀里面会被夹着", source: LIB("哑铃直立划船", "常见错误「拉得太高，肩膀夹着疼」、步骤「手肘不高过肩膀」"), threshold: "〔经验〕" },
  { id: "flyLow", kinds: ["fly"], view: "side", name: "没打开到位", cue: "再打开点", short: "没打开", rank: 2,
    measure: "打开到最高时手腕比肩低多少（按开始前手臂长算）", when: "每一下的最高点", limit: 0.2, bad: "低出手臂长的 20% 以上",
    why: "重量太重或者在赶次数", source: LIB("俯身哑铃反向飞鸟", "步骤「双臂向两侧打开到与身体平齐」"), threshold: "〔经验〕" },
  { id: "armSwing", kinds: ["kickback", "skull"], view: "side", name: "大臂跟着动", cue: "大臂别动", short: "大臂动", rank: 1,
    measure: "肩—肘连线的角度在这一下里变了多少", when: "这一下全程", limit: 20, bad: "变化超过 20°",
    why: "重量太重，用肩膀甩", source: LIB("哑铃俯身臂屈伸", "常见错误「大臂跟着前后甩」、仰卧曲杆臂屈伸的要点「上臂垂直不动」"), threshold: "〔经验〕" },
  { id: "kickShort", kinds: ["kickback"], view: "side", name: "没伸直", cue: "手臂伸直", short: "没伸直", rank: 2,
    measure: "伸到最直时肘的角度", when: "每一下的最高点", limit: 155, bad: "不到 155°",
    why: "重量太重或者在赶次数", source: LIB("哑铃俯身臂屈伸", "步骤「把哑铃往后推到手臂伸直」"), threshold: "〔经验〕" },
];
const RULE = Object.fromEntries(RULES.map((r) => [r.id, r]));

// 关节点编号（MediaPipe 33 点）：左右各一组
const SIDE = {
  l: { ear: 7, s: 11, e: 13, w: 15, h: 23, k: 25, a: 27, heel: 29, toe: 31 },
  r: { ear: 8, s: 12, e: 14, w: 16, h: 24, k: 26, a: 28, heel: 30, toe: 32 },
};
const NOSE = 0;
const P = { ls: 11, rs: 12, lh: 23, rh: 24, lk: 25, rk: 26, la: 27, ra: 28 };
const BODY = [11, 12, 23, 24, 25, 26, 27, 28];  // 深蹲判断全身入镜看这几个点
const BONES = [[11, 12], [11, 23], [12, 24], [23, 24], [11, 13], [13, 15], [12, 14], [14, 16], [23, 25], [25, 27], [24, 26], [26, 28], [27, 29], [29, 31], [27, 31], [28, 30], [30, 32], [28, 32]];
export const SET_IDLE_MS = 12000;  // 做过之后停着不动这么久，算这一组结束
export const AWAY_MS = 5000;       // 做过之后人离开画面这么久，算这一组结束
const PLANK_OFF_MS = 1500;         // 平板支撑离开撑的姿势这么久，算这一组结束
const READY_FRAMES = 15;           // 摆好、稳定这么多帧才开始

// 一组的总结："8 个，第 5、6 个没蹲够"（只说最要紧的那一种问题）
export function summary(reps, faultsByRep) {
  if (!reps) return "";
  let top = null;
  for (const id of [...RULES].sort((a, b) => a.rank - b.rank).map((r) => r.id)) if (faultsByRep.some((f) => f.includes(id))) { top = id; break; }
  if (!top) return `${reps} 个，都不错`;
  const which = faultsByRep.map((f, k) => (f.includes(top) ? k + 1 : 0)).filter(Boolean);
  return `${reps} 个，第 ${which.join("、")} 个${RULE[top].short}`;
}
// 平板支撑的总结："坚持了 45 秒，塌腰 2 次"
export function holdSummary(seconds, counts) {
  const bad = Object.entries(counts).filter(([, n]) => n > 0).map(([id, n]) => `${RULE[id].short} ${n} 次`);
  return `坚持了 ${seconds} 秒${bad.length ? `，${bad.join("、")}` : "，姿势一直不错"}`;
}

// ---- 每类怎么数、怎么判断 ---------------------------------------------------------------------
// view 从哪拍；need 这一侧必须看清的点；ready 摆好了没有；metric 数次数看的那个量；
// rest 停着时这个量是高还是低（深蹲站着时膝角大，臀桥躺着时髋角小）；enter / back 进出一下的阈值；
// frame 每帧记下要判断的东西；judge 这一下做完给出问题。
function kinds(g) {
  const { ang, lean, dist, horiz, line } = g;
  // 往后仰：肩比髋更靠后。朝向看鼻子在耳朵哪一边
  const archBack = (j) => { const face = Math.sign(j.nose.x - j.ear.x) || 1; return (j.h.x - j.s.x) * face > 0 ? lean(j.s, j.h) : 0; };
  const elbow = (j) => ang(j.s, j.e, j.w);
  // 正面拍的（§221）：j.l、j.r 是左右两侧
  const elbows = (j) => (ang(j.l.s, j.l.e, j.l.w) + ang(j.r.s, j.r.e, j.r.w)) / 2;
  const armLift = (j) => [j.l, j.r].reduce((n, x) => n + ((x.s.y - x.w.y) * g.H) / (dist(x.s, x.e) + dist(x.e, x.w) || 1), 0) / 2;  // 0 和肩一样高，1 举直，-1 垂着
  // §222：上身带方向的倾角；上臂的方向；这一下里转了多少（不怕过 ±180°）；卧推类下放用时
  const tilt = (j) => (Math.atan2((j.s.x - j.h.x) * g.W, (j.h.y - j.s.y) * g.H) * 180) / Math.PI;
  const armDir = (j) => (Math.atan2((j.e.y - j.s.y) * g.H, (j.e.x - j.s.x) * g.W) * 180) / Math.PI;
  const span = (r, v) => { if (r.v0 == null) r.v0 = v; const d = ((v - r.v0 + 540) % 360) - 180; r.lo = Math.min(r.lo ?? 0, d); r.hi = Math.max(r.hi ?? 0, d); };
  const spread = (r) => (r.hi ?? 0) - (r.lo ?? 0);
  const drop = (j, r, t) => { if (r.t0 == null) r.t0 = t; const el = elbow(j); if (el < r.minElbow) { r.minElbow = el; r.tMin = t; } };
  const dropSec = (r) => (r.tMin - r.t0) / 1000;
  const run = (r, key, bad) => { r[key + "Run"] = bad ? r[key + "Run"] + 1 : 0; if (r[key + "Run"] >= 4) r[key] = true; };  // 连续 4 帧才算
  return {
    ...Object.fromEntries([["pushup", "a"], ["kneepush", "k"]].map(([k, end]) => [k, {  // 跪姿的身体直线量到膝（§220）
      view: "side", need: ["s", "e", "w", "h", end], rest: "high", enter: 110, back: 150,
      ready: (j) => ang(j.s, j.e, j.w) > 150 && horiz(j.s, j[end]) < 40,
      metric: (j) => ang(j.s, j.e, j.w),
      start: (j) => ({ minElbow: ang(j.s, j.e, j.w), sagRun: 0, pikeRun: 0, sag: false, pike: false }),
      frame: (j, r) => {
        r.minElbow = Math.min(r.minElbow, ang(j.s, j.e, j.w));
        const d = line(j.s, j.h, j[end]);  // 正数：髋在连线下面（塌腰）
        r.sagRun = d > RULE.sag.limit ? r.sagRun + 1 : 0; r.pikeRun = d < -RULE.pike.limit ? r.pikeRun + 1 : 0;
        if (r.sagRun >= 4) r.sag = true;
        if (r.pikeRun >= 4) r.pike = true;
      },
      live: (r) => [r.sag && "sag", r.pike && "pike"],
      judge: (r) => [r.sag && "sag", r.pike && "pike", r.minElbow > RULE.shallowPush.limit && "shallowPush"],
      detail: (r) => `肘最低 ${Math.round(r.minElbow)}°`,
    }])),
    // 杠铃早安式：杠在背上，不看杠离身体远，照罗马尼亚硬拉看膝盖弯太多（§220）
    ...Object.fromEntries(["rdl", "deadlift", "goodmorning"].map((k) => [k, {
      view: "side", need: k === "goodmorning" ? ["s", "h", "k", "a"] : ["s", "w", "h", "k", "a"], rest: "high", enter: 140, back: 165,
      ready: () => true,  // 站着或在地上开始都行：在地上开始时第一帧就进入"折下去"，站起来算第一个
      metric: (j) => ang(j.s, j.h, j.k),
      start: (j) => ({ minKnee: ang(j.h, j.k, j.a), minHip: ang(j.s, j.h, j.k), barRun: 0, bar: false }),
      frame: (j, r) => {
        const hip = ang(j.s, j.h, j.k), leg = dist(j.h, j.k) + dist(j.k, j.a);
        r.minKnee = Math.min(r.minKnee, ang(j.h, j.k, j.a)); r.minHip = Math.min(r.minHip, hip);
        const far = (Math.abs(j.w.x - j.a.x) * g.W) / (leg || 1);
        r.barRun = k !== "goodmorning" && hip < 150 && far > RULE.bar.limit ? r.barRun + 1 : 0;
        if (r.barRun >= 4) r.bar = true;
      },
      live: (r) => [r.bar && "bar"],
      judge: (r) => [r.bar && "bar", k !== "deadlift" && r.minKnee < RULE.squatty.limit && "squatty"],
      detail: (r) => `髋最低 ${Math.round(r.minHip)}°，膝最低 ${Math.round(r.minKnee)}°`,
    }])),
    split: {
      view: "side", need: ["s", "h", "k", "a"], both: ["k", "a"], rest: "high", enter: 120, back: 150,
      ready: (j) => true && ang(j.h, j.front.k, j.front.a) > 150,
      metric: (j) => ang(j.h, j.front.k, j.front.a),
      start: (j) => ({ minKnee: 180, hipAbove: Infinity, leanBottom: 0 }),
      frame: (j, r) => {
        const kn = ang(j.h, j.front.k, j.front.a), leg = dist(j.h, j.front.k) + dist(j.front.k, j.front.a);
        if (kn < r.minKnee) { r.minKnee = kn; r.leanBottom = lean(j.s, j.h); }
        r.hipAbove = Math.min(r.hipAbove, ((j.front.k.y - j.h.y) * g.H) / (leg || 1));
      },
      judge: (r) => [r.hipAbove > RULE.splitDepth.limit && "splitDepth", r.leanBottom > RULE.splitLean.limit && "splitLean"],
      detail: (r) => `前膝最低 ${Math.round(r.minKnee)}°，前倾 ${Math.round(r.leanBottom)}°`,
    },
    bridge: {
      view: "side", need: ["s", "h", "k"], rest: "low", enter: 155, back: 140,
      ready: (j) => horiz(j.s, j.h) < 45 && ang(j.h, j.k, j.a || j.k) < 140 && ang(j.s, j.h, j.k) < 150,
      metric: (j) => ang(j.s, j.h, j.k),
      start: () => ({ maxHip: 0 }),
      frame: (j, r) => { r.maxHip = Math.max(r.maxHip, ang(j.s, j.h, j.k)); },
      judge: (r) => [r.maxHip < RULE.bridgeTop.limit && "bridgeTop"],
      detail: (r) => `顶到 ${Math.round(r.maxHip)}°`,
    },
    press: {
      view: "side", need: ["s", "e", "w", "h"], rest: "low", enter: 140, back: 100,
      ready: (j) => ang(j.s, j.e, j.w) < 100,
      metric: (j) => ang(j.s, j.e, j.w),
      start: () => ({ maxElbow: 0, archRun: 0, arch: false, maxBack: 0 }),
      frame: (j, r) => {
        r.maxElbow = Math.max(r.maxElbow, ang(j.s, j.e, j.w));
        const back = archBack(j);
        r.maxBack = Math.max(r.maxBack, back);
        r.archRun = back > RULE.arch.limit ? r.archRun + 1 : 0;
        if (r.archRun >= 4) r.arch = true;
      },
      live: (r) => [r.arch && "arch"],
      judge: (r) => [r.arch && "arch", r.maxElbow < RULE.lockout.limit && "lockout"],
      detail: (r) => `肘最高 ${Math.round(r.maxElbow)}°，后仰 ${Math.round(r.maxBack)}°`,
    },
    // ---- §222 第三批 ----
    // 卧推：躺着，推直 → 放下 → 推直；屁股抬起和开始前躺好时比
    bench: (() => {
      let base = null;
      return {
        view: "side", need: ["s", "e", "w", "h"], rest: "high", enter: 120, back: 150,
        ready: (j) => { base = base == null ? j.h.y : base * 0.8 + j.h.y * 0.2; return elbow(j) > 150 && horiz(j.s, j.h) < 50 && j.w.y < j.s.y; },
        metric: elbow,
        start: () => ({ minElbow: 180, t0: null, tMin: null, upRun: 0, up: false }),
        frame: (j, r, t) => {
          drop(j, r, t);
          run(r, "up", ((base - j.h.y) * g.H) / (dist(j.s, j.h) || 1) > RULE.hipUp.limit);
        },
        live: (r) => [r.up && "hipUp"],
        judge: (r) => [dropSec(r) < RULE.dropFast.limit && "dropFast", r.up && "hipUp"],
        detail: (r) => `肘最低 ${Math.round(r.minElbow)}°，放下用了 ${dropSec(r).toFixed(1)} 秒`,
      };
    })(),
    // 俯身划船：俯身手臂垂着 → 拉起 → 放直（单臂的拿哑铃那侧对着手机）
    bentrow: {
      view: "side", need: ["s", "e", "w", "h"], rest: "high", enter: 130, back: 155,
      ready: (j) => lean(j.s, j.h) > 30 && elbow(j) > 150 && j.w.y > j.s.y,
      metric: elbow,
      start: () => ({ minElbow: 180 }),
      frame: (j, r) => { r.minElbow = Math.min(r.minElbow, elbow(j)); span(r, tilt(j)); },
      live: (r) => [spread(r) > RULE.torsoRise.limit && "torsoRise"],
      judge: (r) => [spread(r) > RULE.torsoRise.limit && "torsoRise", r.minElbow > RULE.rowShort.limit && "rowShort"],
      detail: (r) => `肘最低 ${Math.round(r.minElbow)}°，上身动了 ${Math.round(spread(r))}°`,
    },
    // 壶铃摇摆：按手腕高低数，荡下去再荡到胸口高度算一个；到胸口那一帧看髋站直没有
    kb: (() => {
      const lift = (j) => ((j.s.y - j.w.y) * g.H) / (dist(j.s, j.e) + dist(j.e, j.w) || 1);
      return {
        view: "side", need: ["s", "e", "w", "h", "k", "a"], rest: "high", enter: -0.6, back: -0.25,
        ready: () => true,  // 站着手垂着时就算"荡下去"了，第一次荡到胸口算第一个
        metric: lift,
        start: () => ({ minKnee: 180, hip: 180 }),
        frame: (j, r) => { r.minKnee = Math.min(r.minKnee, ang(j.h, j.k, j.a)); r.hip = ang(j.s, j.h, j.k); },
        fast: true, turn: 0.1,  // 动作快：做完一下再说（§223）
        judge: (r) => [r.minKnee < RULE.squatty.limit && "squatty", r.hip < RULE.kbArms.limit && "kbArms"],
        detail: (r) => `膝最低 ${Math.round(r.minKnee)}°，壶铃到胸口时髋 ${Math.round(r.hip)}°`,
      };
    })(),
    // 单腿罗马尼亚硬拉：支撑腿是脚踝更低的那条；最低点看两髋错开多少、后腿和上身成不成一条线
    slrdl: {
      view: "side", need: ["s", "h", "k", "a"], both: ["h", "k", "a"], rest: "low", enter: 35, back: 15,
      ready: (j) => lean(j.s, j.h) < 15 && ang(j.h, j.front.k, j.front.a) > 150,
      metric: (j) => lean(j.s, j.h),
      start: () => ({ maxLean: 0, gap: 0, line: 180 }),
      frame: (j, r) => {
        const l = lean(j.s, j.h);
        if (l < r.maxLean) return;
        r.maxLean = l;
        r.gap = (Math.abs(j.front.h.y - j.back.h.y) * g.H) / (dist(j.s, j.h) || 1);
        r.line = ang(j.s, j.h, j.back.a);
      },
      judge: (r) => [r.gap > RULE.hipOpen.limit && "hipOpen", r.line < RULE.seesaw.limit && "seesaw"],
      detail: (r) => `前倾 ${Math.round(r.maxLean)}°，后腿和上身 ${Math.round(r.line)}°`,
    },
    // 悬垂举腿：挂直 → 腿抬起 → 放下
    hangraise: {
      view: "side", need: ["s", "w", "h", "k"], rest: "high", enter: 140, back: 160,
      ready: (j) => j.w.y < j.s.y && ang(j.s, j.h, j.k) > 160,
      metric: (j) => ang(j.s, j.h, j.k),
      start: () => ({ minHip: 180 }),
      frame: (j, r) => { r.minHip = Math.min(r.minHip, ang(j.s, j.h, j.k)); span(r, tilt(j)); },
      live: (r) => [spread(r) > RULE.hangSwing.limit && "hangSwing"],
      judge: (r) => [r.minHip > RULE.hangHalf.limit && "hangHalf", spread(r) > RULE.hangSwing.limit && "hangSwing"],
      detail: (r) => `腿和上身最小 ${Math.round(r.minHip)}°，身体荡了 ${Math.round(spread(r))}°`,
    },
    // 直立划船（正面）：手拉到胸口再放下
    upright: {
      view: "front", need: ["s", "e", "w", "h"], rest: "low", enter: -0.5, back: -0.8,
      ready: (j) => armLift(j) < -0.8,
      metric: armLift,
      start: () => ({ elbowUp: -1, highRun: 0, high: false }),
      frame: (j, r) => {
        const up = Math.max(...[j.l, j.r].map((x) => ((x.s.y - x.e.y) * g.H) / (dist(x.s, x.e) || 1)));  // 正数：肘高过肩
        r.elbowUp = Math.max(r.elbowUp, up);
        run(r, "high", up > RULE.elbowHigh.limit);
      },
      live: (r) => [r.high && "elbowHigh"], turn: 0.1,
      judge: (r) => [r.high && "elbowHigh"],
      detail: (r) => `肘最高比肩${r.elbowUp > 0 ? "高" : "低"}上臂长的 ${Math.abs(Math.round(r.elbowUp * 100))}%`,
    },
    // 俯身反向飞鸟：手腕高低按开始前的手臂长算（手臂朝镜头打开时画面里会变短）
    fly: (() => {
      let arm = null;
      const lift = (j) => ((j.s.y - j.w.y) * g.H) / (arm || 1);
      return {
        view: "side", need: ["s", "e", "w", "h"], rest: "low", enter: -0.5, back: -0.8,
        ready: (j) => { const a = dist(j.s, j.e) + dist(j.e, j.w); arm = arm == null ? a : arm * 0.8 + a * 0.2; return lean(j.s, j.h) > 45 && lift(j) < -0.8; },
        metric: lift,
        start: () => ({ top: -1 }),
        frame: (j, r) => { r.top = Math.max(r.top, lift(j)); span(r, tilt(j)); },
        live: (r) => [spread(r) > RULE.torsoRise.limit && "torsoRise"], turn: 0.1,
        judge: (r) => [spread(r) > RULE.torsoRise.limit && "torsoRise", r.top < -RULE.flyLow.limit && "flyLow"],
        detail: (r) => `手腕最高比肩低手臂长的 ${Math.round(-r.top * 100)}%`,
      };
    })(),
    // 俯身臂屈伸：前臂垂着 → 往后伸直 → 放回
    kickback: {
      view: "side", need: ["s", "e", "w", "h"], rest: "low", enter: 130, back: 105,
      ready: (j) => elbow(j) < 105 && lean(j.s, j.h) > 45,
      metric: elbow,
      start: () => ({ maxElbow: 0 }),
      frame: (j, r) => { r.maxElbow = Math.max(r.maxElbow, elbow(j)); span(r, armDir(j)); },
      live: (r) => [spread(r) > RULE.armSwing.limit && "armSwing"],
      judge: (r) => [spread(r) > RULE.armSwing.limit && "armSwing", r.maxElbow < RULE.kickShort.limit && "kickShort"],
      detail: (r) => `肘最直 ${Math.round(r.maxElbow)}°，大臂动了 ${Math.round(spread(r))}°`,
    },
    // 仰卧臂屈伸：躺着举直 → 放到额头上方 → 举直
    skull: {
      view: "side", need: ["s", "e", "w", "h"], rest: "high", enter: 120, back: 155,
      ready: (j) => elbow(j) > 155 && horiz(j.s, j.h) < 30 && j.w.y < j.s.y,
      metric: elbow,
      start: () => ({ minElbow: 180, t0: null, tMin: null }),
      frame: (j, r, t) => { drop(j, r, t); span(r, armDir(j)); },
      live: (r) => [spread(r) > RULE.armSwing.limit && "armSwing"],
      judge: (r) => [spread(r) > RULE.armSwing.limit && "armSwing", dropSec(r) < RULE.dropFast.limit && "dropFast"],
      detail: (r) => `肘最低 ${Math.round(r.minElbow)}°，大臂动了 ${Math.round(spread(r))}°`,
    },
    // ---- §221 第二批 ----
    // 弯举：肘弯起再放直算一个；身体摆动看肩—髋连线带方向的角度（往前往后都算）
    curl: {
      view: "side", need: ["s", "e", "w", "h"], rest: "high", enter: 120, back: 150,
      ready: (j) => elbow(j) > 150 && lean(j.s, j.h) < 20,
      metric: elbow,
      start: () => ({ minElbow: 180, lo: Infinity, hi: -Infinity, driftRun: 0, drift: false }),
      frame: (j, r) => {
        r.minElbow = Math.min(r.minElbow, elbow(j));
        const tilt = (Math.atan2((j.s.x - j.h.x) * g.W, (j.h.y - j.s.y) * g.H) * 180) / Math.PI;
        r.lo = Math.min(r.lo, tilt); r.hi = Math.max(r.hi, tilt);
        run(r, "drift", lean(j.e, j.s) > RULE.elbowDrift.limit);
      },
      live: (r) => [r.hi - r.lo > RULE.swing.limit && "swing", r.drift && "elbowDrift"],
      judge: (r) => [r.hi - r.lo > RULE.swing.limit && "swing", r.drift && "elbowDrift"],
      detail: (r) => `肘最低 ${Math.round(r.minElbow)}°，身体摆了 ${Math.round(r.hi - r.lo)}°`,
    },
    // 凳上、双杠臂屈伸：撑直 → 屈肘下去 → 撑直；肩比肘低算下太深
    dip: {
      view: "side", need: ["s", "e", "w", "h"], rest: "high", enter: 130, back: 155,
      ready: (j) => elbow(j) > 155 && j.w.y > j.s.y,  // 手撑在肩下面
      metric: elbow,
      start: () => ({ minElbow: 180, deepRun: 0, deep: false }),
      frame: (j, r) => {
        r.minElbow = Math.min(r.minElbow, elbow(j));
        run(r, "deep", ((j.s.y - j.e.y) * g.H) / (dist(j.s, j.e) || 1) > RULE.dipDeep.limit);
      },
      live: (r) => [r.deep && "dipDeep"],
      judge: (r) => [r.deep && "dipDeep"],
      detail: (r) => `肘最低 ${Math.round(r.minElbow)}°`,
    },
    // 颈后臂屈伸：举直 → 放到脑后 → 举直
    overhead: {
      view: "side", need: ["s", "e", "w", "h"], rest: "high", enter: 110, back: 150,
      ready: (j) => elbow(j) > 150 && j.w.y < j.s.y && j.e.y < j.s.y,  // 举在头顶
      metric: elbow,
      start: () => ({ minElbow: 180, archRun: 0, arch: false, fwdRun: 0, fwd: false }),
      frame: (j, r) => {
        r.minElbow = Math.min(r.minElbow, elbow(j));
        run(r, "arch", archBack(j) > RULE.arch.limit);
        run(r, "fwd", lean(j.e, j.s) > RULE.armFwd.limit);
      },
      live: (r) => [r.arch && "arch", r.fwd && "armFwd"],
      judge: (r) => [r.arch && "arch", r.fwd && "armFwd"],
      detail: (r) => `肘最低 ${Math.round(r.minElbow)}°`,
    },
    // 反向划船：躺在杠下手臂伸直 → 拉上去 → 放直
    row: {
      view: "side", need: ["s", "e", "w", "h", "a"], rest: "high", enter: 130, back: 155,
      ready: (j) => elbow(j) > 150 && horiz(j.s, j.a) < 50 && j.w.y < j.s.y,
      metric: elbow,
      start: () => ({ minElbow: 180, sagRun: 0, sag: false }),
      frame: (j, r) => {
        r.minElbow = Math.min(r.minElbow, elbow(j));
        run(r, "sag", line(j.s, j.h, j.a) > RULE.sag.limit);
      },
      live: (r) => [r.sag && "sag"],
      judge: (r) => [r.sag && "sag", r.minElbow > RULE.rowTop.limit && "rowTop"],
      detail: (r) => `肘最低 ${Math.round(r.minElbow)}°`,
    },
    // 提踵：脚跟比脚尖抬高多少（按腿长算），和开始前站着时比
    calf: (() => {
      let base = null;
      const lift = (j) => ((j.toe.y - j.heel.y) * g.H) / (dist(j.h, j.k) + dist(j.k, j.a) || 1);
      return {
        view: "side", need: ["h", "k", "a", "heel", "toe"], rest: "low", enter: 0.025, back: 0.012,
        ready: (j) => {
          const v = lift(j), still = base != null && Math.abs(v - base) < 0.01;
          base = base == null ? v : base * 0.8 + v * 0.2;
          return ang(j.h, j.k, j.a) > 160 && still;
        },
        metric: (j) => lift(j) - base,
        start: () => ({ max: 0 }),
        frame: (j, r) => { r.max = Math.max(r.max, lift(j) - base); },
        turn: 0.01,
        judge: (r) => [r.max < RULE.calfLow.limit && "calfLow"],
        detail: (r) => `脚跟抬高腿长的 ${Math.round(r.max * 100)}%`,
      };
    })(),
    // 仰卧举腿：躺平 → 腿举起 → 放下
    legraise: {
      view: "side", need: ["s", "h", "k", "a"], rest: "high", enter: 140, back: 160,
      ready: (j) => horiz(j.s, j.a) < 25 && ang(j.s, j.h, j.a) > 160,
      metric: (j) => ang(j.s, j.h, j.a),
      start: () => ({ minHip: 180, bendRun: 0, bend: false }),
      frame: (j, r) => {
        r.minHip = Math.min(r.minHip, ang(j.s, j.h, j.a));
        run(r, "bend", ang(j.h, j.k, j.a) < RULE.legBend.limit);
      },
      live: (r) => [r.bend && "legBend"],
      judge: (r) => [r.minHip > RULE.legLow.limit && "legLow", r.bend && "legBend"],
      detail: (r) => `腿和上身最小 ${Math.round(r.minHip)}°`,
    },
    // 引体向上（正面）：吊直 → 拉上去 → 放下；peak 是拉这一下之前放到多直
    pullup: {
      view: "front", need: ["s", "e", "w"], head: true, rest: "high", enter: 110, back: 130,
      ready: (j) => elbows(j) > 150 && j.l.w.y < j.nose.y && j.r.w.y < j.nose.y,  // 吊在杠上
      metric: elbows,
      start: (j, peak) => ({ from: peak ?? 180, over: Infinity, minElbow: 180 }),
      frame: (j, r) => {
        r.minElbow = Math.min(r.minElbow, elbows(j));
        r.over = Math.min(r.over, (j.mouth.y - (j.l.w.y + j.r.w.y) / 2) * g.H);  // 负数：嘴高过手
      },
      judge: (r) => [r.over > RULE.chinUp.limit && "chinUp", r.from < RULE.hangLow.limit && "hangLow"],
      detail: (r) => `肘最低 ${Math.round(r.minElbow)}°，拉之前 ${Math.round(r.from)}°`,
    },
    // 开合跳、星形跳（正面）：手举过肩再放下算一个
    jacks: {
      view: "front", need: ["s", "e", "w", "h", "a"], rest: "low", enter: 0.2, back: -0.4,
      ready: (j) => armLift(j) < -0.6,
      metric: armLift,
      start: () => ({ feet: 0, lift: -1 }),
      frame: (j, r) => {
        r.feet = Math.max(r.feet, (Math.abs(j.l.a.x - j.r.a.x) * g.W) / (dist(j.l.s, j.r.s) || 1));
        r.lift = Math.max(r.lift, armLift(j));
      },
      fast: true, turn: 0.1,  // 动作快：做完一下再说（§223）
      judge: (r) => [r.feet < RULE.feetOut.limit && "feetOut", r.lift < RULE.armsUp.limit && "armsUp"],
      detail: (r) => `脚开到肩宽的 ${r.feet.toFixed(1)} 倍，手举到 ${Math.round(r.lift * 100)}%`,
    },
    // 哥萨克深蹲、侧弓步（正面）：髋往下沉再站回算一个（正面拍时屈的那条腿大腿朝前，膝角看不准）；
    // 屈的那条腿是脚踝离髋中点近的那条；膝盖内扣、前倾都看最低点
    lateral: (() => {
      let base = null;  // 开始前站直时：髋的高度、腿长、躯干长
      const mid = (j) => ({ x: (j.l.h.x + j.r.h.x) / 2, y: (j.l.h.y + j.r.h.y) / 2 });
      const torso = (j) => Math.hypot(((j.l.s.x + j.r.s.x - j.l.h.x - j.r.h.x) / 2) * g.W, ((j.l.s.y + j.r.s.y - j.l.h.y - j.r.h.y) / 2) * g.H);
      const leg = (x) => dist(x.h, x.k) + dist(x.k, x.a);
      return {
        view: "front", need: ["s", "h", "k", "a"], rest: "low", enter: 0.2, back: 0.08,
        ready: (j) => {
          const now = { y: mid(j).y, leg: (leg(j.l) + leg(j.r)) / 2, torso: torso(j) };
          base = base == null ? now : Object.fromEntries(Object.keys(now).map((k) => [k, base[k] * 0.8 + now[k] * 0.2]));
          return Math.min(ang(j.l.h, j.l.k, j.l.a), ang(j.r.h, j.r.k, j.r.a)) > 155;
        },
        metric: (j) => ((mid(j).y - base.y) * g.H) / (base.leg || 1),
        start: () => ({ drop: 0, torso: 1, inward: 0 }),
        frame: (j, r) => {
          const m = mid(j), d = ((m.y - base.y) * g.H) / (base.leg || 1);
          if (d < r.drop) return;
          const bent = Math.abs(j.l.a.x - m.x) < Math.abs(j.r.a.x - m.x) ? j.l : j.r;
          r.drop = d; r.torso = torso(j) / (base.torso || 1);
          r.inward = ((Math.abs(bent.a.x - m.x) - Math.abs(bent.k.x - m.x)) * g.W) / (dist(bent.k, bent.a) || 1);
        },
        turn: 0.05,
        judge: (r) => [r.inward > RULE.sideKnee.limit && "sideKnee", r.torso < RULE.sideLean.limit && "sideLean"],
        detail: (r) => `髋下沉腿长的 ${Math.round(r.drop * 100)}%，躯干剩 ${Math.round(r.torso * 100)}%`,
      };
    })(),
  };
}

// ---- 只看关节点的判断（不碰摄像头、页面，node 里也能跑） ---------------------------------------
// step(lm, t) 每帧调一次，lm 是 33 个点（x、y 是 0–1 的画面坐标，visibility 是认得多确定），t 毫秒。
// 返回这一帧发生的事：status（入镜提示）、start、cue（过程中马上说）、rep（做完一下）、tick（平板支撑的秒数）、set（一组结束）。
export function createAnalyser({ width, height, move = "自重深蹲" }) {
  const kind = MOVES[move] || "squat";
  const W = width, H = height;
  const ang = (a, b, c) => {  // b 处的夹角（度），按画面宽高换算成真实比例
    const v1 = [(a.x - b.x) * W, (a.y - b.y) * H], v2 = [(c.x - b.x) * W, (c.y - b.y) * H];
    const d = (v1[0] * v2[0] + v1[1] * v2[1]) / (Math.hypot(...v1) * Math.hypot(...v2) || 1);
    return (Math.acos(Math.max(-1, Math.min(1, d))) * 180) / Math.PI;
  };
  const lean = (sh, hip) => (Math.atan2(Math.abs((sh.x - hip.x) * W), Math.abs((hip.y - sh.y) * H)) * 180) / Math.PI;  // 和竖直方向的夹角
  const horiz = (a, b) => (Math.atan2(Math.abs((a.y - b.y) * H), Math.abs((a.x - b.x) * W)) * 180) / Math.PI;  // 和水平方向的夹角
  const dist = (a, b) => Math.hypot((a.x - b.x) * W, (a.y - b.y) * H);
  // 肩—髋—踝离一条直线差多少度：正数是髋在肩踝连线下面（塌腰），负数是上面（撅臀）
  const line = (s, h, a) => {
    const sa = [(a.x - s.x) * W, (a.y - s.y) * H], sh = [(h.x - s.x) * W, (h.y - s.y) * H];
    const cross = (sa[0] * sh[1] - sa[1] * sh[0]) * (sa[0] < 0 ? -1 : 1);
    return (cross >= 0 ? 1 : -1) * (180 - ang(s, h, a));
  };
  const HOLD = kind === "plank" || kind === "sideplank";  // 计时的：数姿势对的秒数
  const K = ["squat", "jumpsquat", "raise", "shrugs"].includes(kind) || HOLD ? null : kinds({ ang, lean, dist, horiz, line, W, H })[kind];
  let S;
  const fresh = (keepView = false) => {
    S = { reps: 0, phase: "up", rep: null, faults: [], view: keepView ? S.view : null, started: keepView ? S.started : false,
      ready: 0, viewVote: null, standLeg: null, standHeel: null, standHipY: null, smooth: null, valgusRun: 0,
      lastRepAt: 0, lastSeen: 0, lastStatus: "", ear: keepView ? S.ear : null, peak: null,
      hold: 0, holdFrom: null, offSince: null, holdCounts: kind === "sideplank" ? { hipDrop: 0 } : { sag: 0, pike: 0 }, sagRun: 0, pikeRun: 0, inFault: null, lastTick: 0 };
  };
  fresh();
  const smooth = (lm) => {  // 指数平滑，减少抖动
    const a = 0.5;
    S.smooth = S.smooth ? lm.map((p, i) => { const q = S.smooth[i]; return { x: q.x + a * (p.x - q.x), y: q.y + a * (p.y - q.y), visibility: p.visibility ?? 1 }; })
      : lm.map((p) => ({ x: p.x, y: p.y, visibility: p.visibility ?? 1 }));
    return S.smooth;
  };
  const status = (out, text) => { if (text !== S.lastStatus) { S.lastStatus = text; out.push({ type: "status", text }); } };
  const endSet = (out) => {
    if (HOLD) {
      const seconds = Math.round(S.hold);
      if (seconds) out.push({ type: "set", seconds, counts: { ...S.holdCounts }, text: holdSummary(seconds, S.holdCounts) });
    } else if (S.reps) out.push({ type: "set", reps: S.reps, faults: S.faults.slice(), text: summary(S.reps, S.faults) });
    fresh(true);
  };
  const finishRep = (out, faults, detail, t) => {
    S.phase = "up"; S.reps += 1; S.lastRepAt = t;
    faults = faults.filter(Boolean).sort((a, b) => RULE[a].rank - RULE[b].rank);
    S.faults.push(faults);
    out.push({ type: "rep", n: S.reps, faults, detail });
  };
  // 过程中说口令（§223）：一下里同一个问题只说一次，几个一起出现先说最要紧的；看不清的那一下不说
  const cue = (out, r, ids) => {
    if (!r || r.unclear) return;
    r.said ??= new Set();
    const id = ids.filter((x) => x && !r.said.has(x)).sort((a, b) => RULE[a].rank - RULE[b].rank)[0];
    if (id) { r.said.add(id); out.push({ type: "cue", id }); }
  };
  const ok = (p, v = 0.5) => p && p.visibility >= v && p.y < 0.99 && p.y > 0.01 && p.x > 0.01 && p.x < 0.99;
  // 侧面拍的动作用离镜头近、看得清的那一侧；分腿蹲还要另一条腿，前腿是脚踝更低（离地面更近）的那条
  const take = (lm, s) => Object.fromEntries(Object.entries(SIDE[s]).map(([k, i]) => [k, lm[i]]));
  const pick = (lm, need, both = []) => {
    const score = (s) => need.reduce((n, k) => n + lm[SIDE[s][k]].visibility, 0);
    const near = score("l") >= score("r") ? "l" : "r", far = near === "l" ? "r" : "l";
    const j = take(lm, near), o = take(lm, far);
    if (!need.every((k) => ok(j[k]))) return null;
    if (both.length) {
      if (!both.every((k) => ok(o[k], 0.4))) return null;
      const [fr, bk] = j.a.y >= o.a.y ? [j, o] : [o, j];
      j.front = fr; j.back = bk;
    }
    j.nose = lm[NOSE];
    return j;
  };
  // 正面拍的（§221）：左右两侧都要看清；引体向上还要看清鼻子和嘴
  const pair = (lm, need) => {
    const j = { l: take(lm, "l"), r: take(lm, "r"), nose: lm[NOSE], mouth: { x: (lm[9].x + lm[10].x) / 2, y: (lm[9].y + lm[10].y) / 2, visibility: Math.min(lm[9].visibility, lm[10].visibility) } };
    if (!need.every((k) => ok(j.l[k]) && ok(j.r[k]))) return null;
    return !K.head || (ok(j.nose) && ok(j.mouth)) ? j : null;
  };
  const UPPER = ["raise", "shrugs", "pullup"].includes(kind);  // 只要上半身

  function step(raw, t) {
    const out = [];
    if (!raw) {
      if (HOLD && S.started && S.hold) { offPlank(out, t); return out; }
      if (S.reps && S.lastSeen && t - S.lastSeen > AWAY_MS) endSet(out);
      status(out, UPPER ? "站到画面里，上半身都要拍到" : "站到画面里，全身都要拍到");
      return out;
    }
    S.lastSeen = t;
    const lm = smooth(raw);
    // 正面还是侧面：两肩的距离和躯干长度之比（躺着、趴着的也适用）
    const shW = dist(lm[P.ls], lm[P.rs]);
    const mid = (a, b) => ({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 });
    const torso = dist(mid(lm[P.ls], lm[P.rs]), mid(lm[P.lh], lm[P.rh])) || 1;
    const vote = shW / torso > 0.45 ? "front" : shW / torso < 0.28 ? "side" : "slant";
    if (kind === "squat" || kind === "jumpsquat") return squat(lm, t, out, vote);
    if (kind === "raise" || kind === "shrugs") return raise(lm, t, out, vote);
    const need = kind === "plank" ? ["s", "e", "h", "a"] : kind === "sideplank" ? ["s", "h", "a"] : K.need;
    const front = K?.view === "front";
    const j = front ? pair(lm, need) : pick(lm, need, K?.both || []);
    if (!j) {
      if (S.phase === "down" && S.rep) S.rep.unclear = true;
      if (HOLD && S.started && S.hold) { offPlank(out, t); return out; }
      status(out, S.started ? "看不清，调一下手机" : UPPER ? "往后退一点，上半身和手都要在画面里" : "往后退一点，身体都要在画面里");
      return out;
    }
    if (!S.started) {
      // 侧平板支撑对着镜头侧撑时两肩上下叠着，看起来像正面：不限从哪拍（§220）
      if (kind !== "sideplank" && vote !== (front ? "front" : "side")) { S.ready = 0; status(out, front ? "正对手机，别侧着、别斜着" : "侧对手机，别正对、别斜着"); return out; }
      const set = HOLD ? horiz(j.s, j.a) < 35 : K.ready(j);
      S.ready = set ? S.ready + 1 : 0;
      if (S.ready < READY_FRAMES) { status(out, set ? "摆好别动，马上开始" : HOLD ? "撑好再开始" : "摆好起始姿势"); return out; }
      S.started = true; S.view = vote;
      out.push({ type: "start", view: vote });
      status(out, "开始吧");
    }
    if (HOLD) return plank(j, t, out);
    cycle(j, t, out);
    if (S.reps && S.phase === "up" && t - S.lastRepAt > SET_IDLE_MS) endSet(out);
    return out;
  }

  // 通用的一下：停着（up）→ 离开停着的位置（down）→ 回来，算一个
  // 口令（§223）：live 里的一出现就说；其余的等人开始往回走（离最远处超过 turn）时说；动作快的（fast）做完再说
  function cycle(j, t, out) {
    const v = K.metric(j), low = K.rest === "low";
    if (S.phase === "up") {
      S.peak = S.peak == null ? v : low ? Math.min(S.peak, v) : Math.max(S.peak, v);  // 停着时到了哪（引体向上看放没放直）
      if (low ? v > K.enter : v < K.enter) { S.phase = "down"; S.rep = K.start(j, S.peak); S.rep.far = v; S.peak = null; K.frame(j, S.rep, t); }
      return;
    }
    const r = S.rep;
    K.frame(j, r, t);
    if (!K.fast) {
      if (K.live) cue(out, r, K.live(r));
      r.far = low ? Math.max(r.far, v) : Math.min(r.far, v);
      const turn = K.turn ?? 10;
      if (!r.turned && (low ? v < r.far - turn : v > r.far + turn)) { r.turned = true; cue(out, r, K.judge(r)); }
    }
    if (low ? v < K.back : v > K.back) finishRep(out, S.rep.unclear ? [] : K.judge(S.rep), K.detail(S.rep), t);
  }

  // 平板支撑：姿势对的时候计秒；塌腰、撅臀时提醒、不计；离开撑的姿势一会儿算这一组结束
  function offPlank(out, t) {
    if (S.offSince == null) S.offSince = t;
    if (t - S.offSince > PLANK_OFF_MS) endSet(out);
  }
  function plank(j, t, out) {
    if (horiz(j.s, j.a) > 45) { S.holdFrom = null; offPlank(out, t); return out; }
    S.offSince = null;
    const d = line(j.s, j.h, j.a);
    const [down, up] = kind === "sideplank" ? ["hipDrop", null] : ["sag", "pike"];  // 侧平板只看髋往下塌
    S.sagRun = d > RULE[down].limit ? S.sagRun + 1 : 0; S.pikeRun = up && d < -RULE[up].limit ? S.pikeRun + 1 : 0;
    const fault = S.sagRun >= 4 ? down : S.pikeRun >= 4 ? up : null;
    if (fault && fault !== S.inFault) { S.holdCounts[fault] += 1; out.push({ type: "cue", id: fault }); }
    S.inFault = fault;
    if (fault || S.sagRun || S.pikeRun) { S.holdFrom = null; return out; }
    if (S.holdFrom != null) S.hold += (t - S.holdFrom) / 1000;
    S.holdFrom = t;
    const sec = Math.floor(S.hold);
    if (sec > S.lastTick) { S.lastTick = sec; out.push({ type: "tick", seconds: sec }); }
    return out;
  }

  // 平举：正面拍，只要上半身。抬到和肩差不多高再放下算一个；耸肩和开始时放松站着比
  function raise(lm, t, out, vote) {
    const need = [7, 8, 11, 12, 13, 14, 15, 16, 23, 24];
    if (!need.every((i) => ok(lm[i]))) {
      if (S.phase === "down" && S.rep) S.rep.unclear = true;
      status(out, S.started ? "看不清，调一下手机" : "往后退一点，上半身都要在画面里");
      return out;
    }
    const arm = (s, e, w) => dist(lm[s], lm[e]) + dist(lm[e], lm[w]);
    const lift = ((lm[11].y - lm[15].y) * H / arm(11, 13, 15) + (lm[12].y - lm[16].y) * H / arm(12, 14, 16)) / 2;  // 0 是和肩一样高，-1 是垂在身边
    const ear = ((lm[11].y - lm[7].y) + (lm[12].y - lm[8].y)) / 2 * H / dist(lm[11], lm[12]);
    if (!S.started) {
      if (vote !== "front") { S.ready = 0; status(out, "正对手机站"); return out; }
      const down = lift < -0.6 && (kind !== "shrugs" || S.ready < 3 || Math.abs(ear / (S.ear || ear) - 1) < 0.08);  // 耸肩要先放松站稳
      S.ready = down ? S.ready + 1 : 0;
      S.ear = down ? (S.ear == null ? ear : S.ear * 0.8 + ear * 0.2) : S.ear;
      if (S.ready < READY_FRAMES) { status(out, down ? "站好别动，马上开始" : "手臂放下，肩膀放松"); return out; }
      S.started = true; S.view = "front";
      out.push({ type: "start", view: "front" });
      status(out, kind === "shrugs" ? "正面：数耸肩" : "正面：看耸肩");
    }
    if (kind === "shrugs") return shrugs(ear, t, out);
    if (S.phase === "up") {
      if (lift < -0.6) S.ear = S.ear * 0.95 + ear * 0.05;  // 放下休息时慢慢跟上
      if (lift > -0.25) { S.phase = "down"; S.rep = { top: lift, shrugRun: 0, shrug: false, unclear: false }; }
    } else {
      const r = S.rep;
      r.top = Math.max(r.top, lift);
      r.shrugRun = ear < S.ear * RULE.shrug.limit ? r.shrugRun + 1 : 0;
      if (r.shrugRun >= 4) r.shrug = true;
      cue(out, r, [r.shrug && "shrug"]);  // §223：一耸就说
      if (lift < -0.6) finishRep(out, r.unclear ? [] : [r.shrug && "shrug"], `抬到肩高的 ${Math.round((1 + r.top) * 100)}%`, t);
    }
    if (S.reps && S.phase === "up" && t - S.lastRepAt > SET_IDLE_MS) endSet(out);
    return out;
  }

  // 哑铃耸肩（§220）：耳朵到肩的距离比开始时放松站着缩到 88% 以下算耸起来，回到 95% 以上算一个
  function shrugs(ear, t, out) {
    const ratio = ear / S.ear;
    if (S.phase === "up") {
      if (ratio > 0.95) S.ear = S.ear * 0.95 + ear * 0.05;
      if (ratio < 0.88) { S.phase = "down"; S.rep = { min: ratio, unclear: false }; }
    } else {
      S.rep.min = Math.min(S.rep.min, ratio);
      if (!S.rep.turned && ratio > S.rep.min + 0.03) { S.rep.turned = true; cue(out, S.rep, [S.rep.min > RULE.shrugLow.limit && "shrugLow"]); }
      if (ratio > 0.95) finishRep(out, S.rep.unclear ? [] : [S.rep.min > RULE.shrugLow.limit && "shrugLow"], `最高时还剩 ${Math.round(S.rep.min * 100)}%`, t);
    }
    if (S.reps && S.phase === "up" && t - S.lastRepAt > SET_IDLE_MS) endSet(out);
    return out;
  }

  // 深蹲（§218）：侧面、正面两套
  function squat(lm, t, out, vote) {
    // 有一侧从肩到脚踝都看得清就算入镜：侧面拍时离镜头远的那一侧本来就被挡着（11-16 实测）
    const body = BODY.filter((_, k) => k % 2 === 0).every((i) => ok(lm[i])) || BODY.filter((_, k) => k % 2 === 1).every((i) => ok(lm[i]));
    if (!body) {
      if (S.phase === "down" && S.rep) S.rep.unclear = true;  // 这一下有一段没看清，不判断问题
      status(out, S.started ? "看不清，调一下手机" : "往后退一点，头到脚都要在画面里");
      return out;
    }
    const kL = ang(lm[P.lh], lm[P.lk], lm[P.la]), kR = ang(lm[P.rh], lm[P.rk], lm[P.ra]);
    if (!S.started) {
      if (vote === "slant") { S.ready = 0; status(out, "侧着或正对手机站，别斜着"); return out; }
      const standing = Math.max(kL, kR) > 160;
      S.ready = standing && vote === S.viewVote ? S.ready + 1 : standing ? 1 : 0;
      S.viewVote = vote;
      if (S.ready < READY_FRAMES) { status(out, standing ? "站好别动，马上开始" : "先站直"); return out; }
      S.started = true; S.view = vote;
      out.push({ type: "start", view: vote });
      status(out, vote === "side" ? "侧面：看深度、前倾、脚跟" : "正面：看膝盖内扣");
    }
    if (S.view === "side") squatSide(lm, t, out); else squatFront(lm, t, out);
    if (S.reps && S.phase === "up" && t - S.lastRepAt > SET_IDLE_MS) endSet(out);
    return out;
  }

  function squatSide(lm, t, out) {
    const j = pick(lm, ["s", "h", "k", "a"]) || Object.fromEntries(Object.entries(SIDE.l).map(([k, i]) => [k, lm[i]]));
    const { s: sh, h: hip, k: knee, a: ank, heel, toe } = j;
    const knA = ang(hip, knee, ank), lk = lean(sh, hip), leg = dist(hip, knee) + dist(knee, ank);
    // 脚跟抬高用站着时量的腿长当尺子（蹲下时画面里腿会变短）
    if (knA > 160) S.standLeg = S.standLeg ? S.standLeg * 0.9 + leg * 0.1 : leg;
    const heelUp = S.standLeg ? -(heel.y * H) / S.standLeg : 0;
    if (S.phase === "up") {
      if (knA > 160) S.standHeel = S.standHeel == null ? heelUp : S.standHeel * 0.9 + heelUp * 0.1;
      if (knA < 120) { S.phase = "down"; S.rep = { minKnee: knA, deep: false, leanBottom: lk, heelRun: 0, heel: false, unclear: false }; }
      return;
    }
    const r = S.rep;
    if (knA < r.minKnee) { r.minKnee = knA; r.leanBottom = lk; }
    if ((hip.y - knee.y) * H > -RULE.depth.limit * leg) r.deep = true;  // 髋差不多低到膝
    const seen = knA > 70 && heel.visibility > 0.75 && toe.visibility > 0.75;
    r.heelRun = S.standHeel != null && seen && heelUp - S.standHeel > RULE.heel.limit ? r.heelRun + 1 : 0;
    if (r.heelRun >= 4) r.heel = true;
    if (kind !== "jumpsquat") {  // §223：脚跟一离地就说；没蹲够、前倾多在刚往上起时说
      cue(out, r, [r.heel && "heel"]);
      if (!r.turned && knA > r.minKnee + 10) { r.turned = true; cue(out, r, [!r.deep && "depth", r.leanBottom > RULE.lean.limit && "lean"]); }
    }
    // 深蹲跳只数次数：跳起来脚跟本来就离地，蹲多深也不固定（§220）
    const faults = r.unclear || kind === "jumpsquat" ? [] : [!r.deep && "depth", r.leanBottom > RULE.lean.limit && "lean", r.heel && "heel"];
    if (knA > 160) finishRep(out, faults, `最低 ${Math.round(r.minKnee)}°，前倾 ${Math.round(r.leanBottom)}°`, t);
  }

  function squatFront(lm, t, out) {
    const hipY = (lm[P.lh].y + lm[P.rh].y) / 2;
    const leg = (dist(lm[P.lh], lm[P.lk]) + dist(lm[P.lk], lm[P.la]) + dist(lm[P.rh], lm[P.rk]) + dist(lm[P.rk], lm[P.ra])) / 2 / H;
    if (S.phase === "up") S.standHipY = S.standHipY == null || hipY < S.standHipY ? hipY : S.standHipY * 0.98 + hipY * 0.02;
    const drop = (hipY - S.standHipY) / (leg || 1);
    const kneeW = Math.abs(lm[P.lk].x - lm[P.rk].x), ankW = Math.abs(lm[P.la].x - lm[P.ra].x);
    if (S.phase === "up") {
      if (drop > 0.25) { S.phase = "down"; S.rep = { valgus: false, maxDrop: drop, unclear: false }; S.valgusRun = 0; }
      return;
    }
    const r = S.rep;
    r.maxDrop = Math.max(r.maxDrop, drop);
    S.valgusRun = kneeW < ankW * RULE.valgus.limit ? S.valgusRun + 1 : 0;
    if (S.valgusRun >= 4 && !r.valgus) { r.valgus = true; out.push({ type: "cue", id: "valgus" }); }  // 会伤膝盖：过程中就说
    if (drop < 0.08) finishRep(out, r.unclear ? [] : [r.valgus && "valgus"], `髋下降了腿长的 ${Math.round(r.maxDrop * 100)}%`, t);
  }

  return {
    step, kind,
    reset: () => fresh(),
    endSet: () => { const out = []; endSet(out); return out; },
    get reps() { return S.reps; },
    get seconds() { return Math.round(S.hold); },
  };
}

// ---- 语音：报数、口令、总结。三档：mute 静音 / count 只报数 / cue 报数加提醒 ---------------------------
export function createVoice(speak) {
  let mode = "cue", lastFault = null, saidThisRep = false;
  const say = (text, urgent = false) => { if (mode !== "mute" && text) speak(text, urgent); };  // urgent：口令、总结，不让报数打断
  return {
    get mode() { return mode; },
    set mode(m) { mode = m; },
    // 一帧里的事件 → 说什么。同一个问题连续出现只说一次；做对了只报数；平板支撑每 10 秒报一次
    hear(events) {
      for (const e of events) {
        if (e.type === "start") say("好，开始");
        else if (e.type === "cue" && mode === "cue" && e.id !== lastFault) { say(RULE[e.id].cue, true); lastFault = e.id; saidThisRep = true; }
        else if (e.type === "tick") { if (e.seconds % 10 === 0) say(`${e.seconds} 秒`); lastFault = null; }  // 姿势回来了，再塌腰要再提醒
        else if (e.type === "rep") {
          const top = e.faults[0] || null;
          if (mode === "cue" && top && top !== lastFault && !saidThisRep) say(`${e.n}，${RULE[top].cue}`, true);
          else say(String(e.n));
          lastFault = top; saidThisRep = false;
        } else if (e.type === "set") { say(e.text, true); lastFault = null; }
      }
    },
  };
}

// ---- 摄像头、识别、画火柴人 -----------------------------------------------------------------
const BASE = "/iron/pose";
let loading = null, lastTs = 0;
const stamp = (x) => (lastTs = Math.max(lastTs + 1, Math.round(x)));  // 识别要求时间戳一直往上走（摄像头和视频共用一个识别器）
function painter(canvas) {
  const ctx = canvas.getContext("2d");
  return (lm) => {
    const W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);
    if (!lm) return;
    ctx.lineWidth = Math.max(3, W / 200); ctx.strokeStyle = "rgba(242,96,63,.95)";
    for (const [a, b] of BONES) { if (lm[a].visibility < 0.4 || lm[b].visibility < 0.4) continue; ctx.beginPath(); ctx.moveTo(lm[a].x * W, lm[a].y * H); ctx.lineTo(lm[b].x * W, lm[b].y * H); ctx.stroke(); }
    ctx.fillStyle = "#fff";
    for (const i of [11, 12, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32]) { if (lm[i].visibility < 0.4) continue; ctx.beginPath(); ctx.arc(lm[i].x * W, lm[i].y * H, ctx.lineWidth * 1.3, 0, 7); ctx.fill(); }
  };
}
function landmarker() {
  if (!loading) loading = (async () => {
    const { PoseLandmarker, FilesetResolver } = await import(`${BASE}/vision_bundle.mjs`);
    const vision = await FilesetResolver.forVisionTasks(BASE);
    const make = (delegate) => PoseLandmarker.createFromOptions(vision, {
      baseOptions: { modelAssetPath: `${BASE}/pose_landmarker_lite.task`, delegate }, runningMode: "VIDEO", numPoses: 1 });
    try { return { lm: await make("GPU"), make }; } catch { return { lm: await make("CPU"), make, cpu: true }; }
  })().catch((err) => { loading = null; throw err; });
  return loading;
}

// 朗读排队（§223）：新的一句一般盖掉没说完的，免得越积越晚；但正在说口令或总结时，报数排在后面，不打断
export function createSpeaker(synth, Utter) {
  let urgentBusy = false, cur = 0;
  return (text, urgent = false) => {
    const u = new Utter(text);
    u.lang = "zh-CN"; u.rate = 1.15;
    if (urgent || !(synth.speaking && urgentBusy)) {
      synth.cancel();
      urgentBusy = urgent;
      const id = ++cur;
      u.onend = () => { if (id === cur) urgentBusy = false; };  // 被盖掉的那句结束时不算数
    }
    synth.speak(u);
  };
}

// 打开摄像头开始看 move 这个动作。on 里的回调：status(text)、event(e)、fps(n)。返回 { stop, finishSet, voice, reps }
export async function startCoach({ video, canvas, move, on = {}, src = null }) {
  if ("speechSynthesis" in window) speechSynthesis.speak(new SpeechSynthesisUtterance(""));  // 这次点击顺手解锁朗读（iPhone 要先点一下才能出声）
  on.status?.("正在加载识别…");
  const model = await landmarker();
  let stream = null;
  if (src) { video.src = src; video.loop = true; video.muted = true; }  // 自测用：拿一段视频代替摄像头
  else {
    if (!navigator.mediaDevices?.getUserMedia) throw new Error(window.isSecureContext ? "这个浏览器打不开摄像头" : "要用 https 打开才能用摄像头");
    try { stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "user", width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false }); }
    catch (err) { throw new Error(err.name === "NotAllowedError" ? "没给摄像头权限，在浏览器设置里打开后再试" : "打不开摄像头，可能被别的应用占着"); }
    video.srcObject = stream;
  }
  video.playsInline = true;
  await video.play();
  const mirror = !src;  // 前置摄像头像照镜子一样显示
  canvas.style.transform = video.style.transform = mirror ? "scaleX(-1)" : "";
  let wake = null;
  try { wake = await navigator.wakeLock?.request("screen"); } catch { /* 不支持就算了 */ }
  const voice = createVoice("speechSynthesis" in window ? createSpeaker(window.speechSynthesis, window.SpeechSynthesisUtterance) : () => {});
  let an = null, running = true, lastT = -1, frames = 0, fpsAt = performance.now();
  const draw = painter(canvas);
  const emit = (events) => { voice.hear(events); for (const e of events) { if (e.type === "status") on.status?.(e.text); else on.event?.(e); } };
  const loop = () => {
    if (!running) return;
    if (video.readyState >= 2 && video.currentTime !== lastT) {
      lastT = video.currentTime;
      if (canvas.width !== video.videoWidth) { canvas.width = video.videoWidth; canvas.height = video.videoHeight; an = null; }
      if (!an) an = createAnalyser({ width: canvas.width, height: canvas.height, move });
      let res = null;
      try { res = model.lm.detectForVideo(video, stamp(performance.now())); }
      catch { if (!model.cpu) { model.cpu = true; model.make("CPU").then((x) => { model.lm = x; requestAnimationFrame(loop); }); return; } }
      const lm = res?.landmarks?.[0] || null;
      draw(lm);
      emit(an.step(lm, performance.now()));
      frames += 1;
      const now = performance.now();
      if (now - fpsAt >= 1000) { on.fps?.(Math.round((frames * 1000) / (now - fpsAt))); frames = 0; fpsAt = now; }
    }
    requestAnimationFrame(loop);
  };
  requestAnimationFrame(loop);
  return {
    voice,
    get reps() { return an ? an.reps : 0; },
    // 点"完成这组"：按现在数到的结束这一组，说总结，返回次数（平板支撑返回秒数）
    finishSet() { const ev = an ? an.endSet() : []; emit(ev); const e = ev.find((x) => x.type === "set"); return e ? e.seconds ?? e.reps : 0; },
    stop() {
      running = false;
      stream?.getTracks().forEach((tr) => tr.stop());
      video.pause(); video.srcObject = null; video.removeAttribute("src");
      wake?.release?.().catch(() => {});
      if ("speechSynthesis" in window) speechSynthesis.cancel();
    },
  };
}

// ---- 训练视频分析（§224）：同一套规则逐帧看用户选的一段视频；结果写成一段文字交给管家 -------------------
export const VIDEO_MAX_S = 180;  // 只看前 3 分钟
export const VIDEO_TAG = "动作视频分析";
const VIEW_ZH = { side: "侧面", front: "正面", slant: "斜着" };

// 把每一帧的事件攒成结果：次数按整段视频连着数，记下每一下、每个口令出在第几秒
export function createRecorder(move) {
  const r = { move, view: null, started: false, reps: [], cues: [], sets: [], frames: 0, seen: 0, seconds: 0, status: "" };
  const add = (events, at) => {
    for (const e of events) {
      if (e.type === "status") r.status = e.text;
      else if (e.type === "start") { r.started = true; r.view = e.view; }
      else if (e.type === "rep") r.reps.push({ n: r.reps.length + 1, at, faults: e.faults, detail: e.detail });
      else if (e.type === "cue") r.cues.push({ id: e.id, at });
      else if (e.type === "set") r.sets.push({ ...e, at });
    }
  };
  return {
    frame(lm, events, at) { r.frames += 1; if (lm) r.seen += 1; r.seconds = Math.max(r.seconds, at); add(events, at); },
    add,
    result: r,
  };
}

// 结果写成交给管家的一段文字：每一下的问题、每种问题量的是什么和依据（数都是规则算的，不是模型看的）
export function videoReport(r) {
  const hold = ["plank", "sideplank"].includes(MOVES[r.move]);
  const sec = (x) => `第 ${Math.max(1, Math.round(x))} 秒`;
  const lines = [`〔${VIDEO_TAG}〕`,
    `动作：${r.move}；从${VIEW_ZH[r.view] || "—"}拍；视频 ${Math.round(r.seconds)} 秒，看清了 ${Math.round((r.seen / (r.frames || 1)) * 100)}% 的画面`];
  let ids = [];
  if (hold) {
    const seconds = r.sets.reduce((n, x) => n + (x.seconds || 0), 0), counts = {};
    for (const c of r.cues) counts[c.id] = (counts[c.id] || 0) + 1;
    lines.push(`结果：${holdSummary(seconds, counts)}`);
    for (const c of r.cues) lines.push(`${sec(c.at)}：${RULE[c.id].name}`);
    ids = [...new Set(r.cues.map((c) => c.id))];
  } else {
    lines.push(`结果：${summary(r.reps.length, r.reps.map((x) => x.faults)) || "没数到完整的一下"}`);
    for (const x of r.reps) if (x.faults.length) lines.push(`第 ${x.n} 个（${sec(x.at)}）：${x.faults.map((id) => RULE[id].name).join("、")}（${x.detail}）`);
    const good = r.reps.filter((x) => !x.faults.length).map((x) => x.n);
    if (good.length && good.length < r.reps.length) lines.push(`没查出问题的：第 ${good.join("、")} 个`);
    ids = [...new Set(r.reps.flatMap((x) => x.faults))];
  }
  if (ids.length) {
    lines.push("这些问题怎么算的：");
    for (const id of ids) { const x = RULE[id]; lines.push(`- ${x.name}：${x.measure}，${x.bad}算；依据${x.source}；阈值${x.threshold}`); }
  }
  lines.push(`〔/${VIDEO_TAG}〕`);
  return lines.join("\n");
}

// 逐帧看一段视频。on：status(text)、event(e, 秒)、progress(0–1)。返回 { done: Promise<结果>, stop }
export async function analyseVideo({ file, move, video, canvas, on = {} }) {
  on.status?.("正在加载识别…");
  const model = await landmarker();
  const url = URL.createObjectURL(file);
  video.muted = true; video.playsInline = true; video.loop = false; video.src = url;
  await new Promise((ok, bad) => { video.onloadedmetadata = ok; video.onerror = () => bad(new Error("这个视频放不了，换成 MP4 再试")); });
  const end = Math.min(Number.isFinite(video.duration) ? video.duration : VIDEO_MAX_S, VIDEO_MAX_S);
  canvas.width = video.videoWidth; canvas.height = video.videoHeight;
  const an = createAnalyser({ width: video.videoWidth, height: video.videoHeight, move });
  const rec = createRecorder(move), draw = painter(canvas), t0 = performance.now();
  let finished = false, last = -1, resolve;
  const done = new Promise((ok) => { resolve = ok; });
  const finish = () => {
    if (finished) return;
    finished = true; video.pause(); video.onended = null;
    rec.add(an.endSet(), Math.max(last, 0));
    resolve(rec.result);
  };
  const next = () => (video.requestVideoFrameCallback ? video.requestVideoFrameCallback(step) : requestAnimationFrame(() => step(0, null)));
  function step(_now, meta) {
    if (finished) return;
    const at = meta?.mediaTime ?? video.currentTime;
    if (at > last) {
      last = at;
      let res = null;
      try { res = model.lm.detectForVideo(video, stamp(t0 + at * 1000)); } catch { /* 这一帧认不出就跳过 */ }
      const lm = res?.landmarks?.[0] || null;
      draw(lm);
      const ev = an.step(lm, at * 1000);
      rec.frame(lm, ev, at);
      for (const e of ev) { if (e.type === "status") on.status?.(e.text); else on.event?.(e, at); }
      on.progress?.(Math.min(1, at / end));
    }
    if (video.ended || video.currentTime >= end) finish(); else next();
  }
  video.onended = finish;
  await video.play();
  next();
  return { done, stop: finish };
}
