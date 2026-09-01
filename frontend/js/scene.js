// 3D 双房间数字孪生沙盒（ES Module + GLTFLoader 版本）
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

const HomeScene = (() => {
  let scene, camera, renderer, controls;
  let deviceMeshes = {};
  let environment = { temp: 25, humidity: 50, light: 400, motion_living: true, motion_bedroom: false, air_quality: 85 };
  let animatables = [];
  let loadedModels = {};
  const loader = new GLTFLoader();

  // 模型配置：路径 + 位置 + targetSize(目标最大边长米) + 旋转
  // 布局：客厅(x<0)沙发与电视面对面；卧室(x>0)床靠后墙
  const MODEL_CONFIG = {
    couch:    { path: 'models/couch_medium.glb',  pos: [-3.5, 0, -0.8],  targetSize: 2.3, rot: [0, Math.PI, 0] },
    coffee:   { path: 'models/coffee_table.glb',  pos: [-3.5, 0, -2.2],  targetSize: 1.0, rot: [0, 0, 0] },
    tvstand:  { path: 'models/tv_stand.glb',      pos: [-3.5, 0, -4.3],  targetSize: 1.6, rot: [0, 0, 0] },
    bed:      { path: 'models/bed_double.glb',    pos: [3.5, 0, -1.5],   targetSize: 1.8, rot: [0, Math.PI, 0] },
    nightstand:{path: 'models/nightstand.glb',    pos: [1.8, 0, -1.5],   targetSize: 0.5, rot: [0, Math.PI, 0] },
    wardrobe: { path: 'models/wardrobe.glb',      pos: [6.2, 0, -2.5],   targetSize: 1.5, rot: [0, 0, 0] },
    // 装饰模型
    plant1:   { path: 'models/plant_fiddle.glb',  pos: [-6.2, 0, 2.5],   targetSize: 1.5, rot: [0, 0, 0] },
    plant2:   { path: 'models/plant_fiddle.glb',  pos: [6.2, 0, 2.5],    targetSize: 1.2, rot: [0, Math.PI, 0] },
    bookshelf:{ path: 'models/bookshelf.glb',     pos: [-1.3, 0, -4.4],  targetSize: 1.2, rot: [0, 0, 0] },
    floorlamp:{ path: 'models/floor_lamp.glb',    pos: [-5.8, 0, -0.5],  targetSize: 1.6, rot: [0, Math.PI/4, 0] },
  };

  async function loadModel(key) {
    const cfg = MODEL_CONFIG[key];
    return new Promise((resolve) => {
      loader.load(cfg.path, (gltf) => {
        const model = gltf.scene;
        // 计算 bounding box 自动适配尺寸
        const box = new THREE.Box3().setFromObject(model);
        const size = box.getSize(new THREE.Vector3());
        const targetSize = cfg.targetSize || 2.0; // 目标最大尺寸（米）
        const autoScale = targetSize / Math.max(size.x, size.y, size.z);
        model.scale.setScalar(autoScale);
        model.position.set(...cfg.pos);
        model.rotation.set(...cfg.rot);
        model.traverse((child) => {
          if (child.isMesh) {
            child.castShadow = true;
            child.receiveShadow = true;
          }
        });
        loadedModels[key] = model;
        window.__modelInfo = window.__modelInfo || {};
        window.__modelInfo[key] = { size: {x: size.x, y: size.y, z: size.z}, scale: autoScale };
        resolve(model);
      }, undefined, (err) => {
        console.warn(`模型加载失败 ${key}:`, err);
        resolve(null);
      });
    });
  }

  function init(container) {
    scene = new THREE.Scene();
    scene.background = new THREE.Color(0x1a1a2e);

    camera = new THREE.PerspectiveCamera(50, container.clientWidth / container.clientHeight, 0.1, 200);
    camera.position.set(0, 8, 10);
    camera.lookAt(0, 0, -1);

    renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setSize(container.clientWidth, container.clientHeight);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    container.appendChild(renderer.domElement);

    // 环境光
    const ambient = new THREE.AmbientLight(0xffffff, 0.5);
    scene.add(ambient);
    scene.userData.ambient = ambient;

    // 主方向光（模拟日光，从窗户方向射入）
    const dirLight = new THREE.DirectionalLight(0xfff5e6, 0.9);
    dirLight.position.set(-8, 10, 3);
    dirLight.castShadow = true;
    dirLight.shadow.mapSize.set(2048, 2048);
    dirLight.shadow.camera.left = -10;
    dirLight.shadow.camera.right = 10;
    dirLight.shadow.camera.top = 10;
    dirLight.shadow.camera.bottom = -10;
    dirLight.shadow.bias = -0.001;
    scene.add(dirLight);
    scene.userData.dirLight = dirLight;

    // 补光（从另一侧，减少阴影过暗）
    const fillLight = new THREE.DirectionalLight(0xe6f0ff, 0.3);
    fillLight.position.set(5, 8, -5);
    scene.add(fillLight);

    buildRoom();
    buildDevices();
    initOrbitControls(container);

    // 加载 GLB 家具模型
    Promise.all(Object.keys(MODEL_CONFIG).map(loadModel)).then((models) => {
      models.forEach((m) => { if (m) scene.add(m); });
      console.log('家具模型加载完成');
    });

    window.addEventListener('resize', () => {
      camera.aspect = container.clientWidth / container.clientHeight;
      camera.updateProjectionMatrix();
      renderer.setSize(container.clientWidth, container.clientHeight);
    });

    animate();
  }

  function buildRoom() {
    // 木纹地板（更精致的程序化纹理）
    const floorGeo = new THREE.PlaneGeometry(14, 10);
    const floorCanvas = document.createElement('canvas');
    floorCanvas.width = 512; floorCanvas.height = 512;
    const fctx = floorCanvas.getContext('2d');
    fctx.fillStyle = '#c9a97e'; fctx.fillRect(0,0,512,512);
    // 木板
    const plankH = 64;
    for (let row = 0; row < 8; row++) {
      const y = row * plankH;
      const offset = row % 2 === 0 ? 0 : 128;
      for (let col = -1; col < 5; col++) {
        const x = col * 128 + offset;
        // 木板底色变化
        const shade = Math.random() * 20 - 10;
        fctx.fillStyle = `rgb(${201+shade}, ${169+shade}, ${126+shade})`;
        fctx.fillRect(x+1, y+1, 126, plankH-2);
        // 木纹
        fctx.strokeStyle = `rgba(160, 120, 70, ${0.2 + Math.random()*0.2})`;
        fctx.lineWidth = 1;
        for (let g = 0; g < 5; g++) {
          fctx.beginPath();
          const gy = y + 10 + g * 12 + Math.random() * 5;
          fctx.moveTo(x+5, gy);
          fctx.bezierCurveTo(x+40, gy+3, x+80, gy-3, x+123, gy);
          fctx.stroke();
        }
      }
      // 木板缝隙
      fctx.strokeStyle = '#9a7a50';
      fctx.lineWidth = 2;
      fctx.beginPath(); fctx.moveTo(0, y); fctx.lineTo(512, y); fctx.stroke();
    }
    const floorTex = new THREE.CanvasTexture(floorCanvas);
    floorTex.wrapS = floorTex.wrapT = THREE.RepeatWrapping;
    floorTex.repeat.set(2, 2);
    const floor = new THREE.Mesh(floorGeo, new THREE.MeshStandardMaterial({ map: floorTex, roughness: 0.6, metalness: 0.05 }));
    floor.rotation.x = -Math.PI / 2;
    floor.receiveShadow = true;
    scene.add(floor);

    const wallMat = new THREE.MeshStandardMaterial({ color: 0xf0ebe3, roughness: 0.9 });
    const darkWallMat = new THREE.MeshStandardMaterial({ color: 0xe5ddd2, roughness: 0.9 });
    // 客厅 accent 墙（电视背景墙）
    const accentWallMat = new THREE.MeshStandardMaterial({ color: 0xd4c5b0, roughness: 0.85 });

    // 后墙
    const backWall = new THREE.Mesh(new THREE.BoxGeometry(14, 3.5, 0.2), wallMat);
    backWall.position.set(0, 1.75, -5);
    backWall.receiveShadow = true;
    scene.add(backWall);

    // 电视背景墙木饰面（客厅区域）
    const accentPanel = new THREE.Mesh(new THREE.BoxGeometry(4.5, 2.8, 0.06), accentWallMat);
    accentPanel.position.set(-3.5, 1.6, -4.88);
    scene.add(accentPanel);
    // 木饰面竖条装饰
    for (let i = 0; i < 5; i++) {
      const slat = new THREE.Mesh(new THREE.BoxGeometry(0.08, 2.6, 0.04), new THREE.MeshStandardMaterial({ color: 0xb8a080, roughness: 0.7 }));
      slat.position.set(-5.5 + i * 1.0, 1.6, -4.84);
      scene.add(slat);
    }

    // 左墙
    const leftWall = new THREE.Mesh(new THREE.BoxGeometry(0.2, 3.5, 10), wallMat);
    leftWall.position.set(-7, 1.75, 0);
    leftWall.receiveShadow = true;
    scene.add(leftWall);

    // 右墙
    const rightWall = new THREE.Mesh(new THREE.BoxGeometry(0.2, 3.5, 10), wallMat);
    rightWall.position.set(7, 1.75, 0);
    rightWall.receiveShadow = true;
    scene.add(rightWall);

    // 中间隔墙（带1.2米宽门洞）
    const wallLeft = new THREE.Mesh(new THREE.BoxGeometry(0.2, 3.5, 4.4), darkWallMat);
    wallLeft.position.set(0, 1.75, -2.8);
    scene.add(wallLeft);
    const wallRight = new THREE.Mesh(new THREE.BoxGeometry(0.2, 3.5, 4.4), darkWallMat);
    wallRight.position.set(0, 1.75, 2.8);
    scene.add(wallRight);
    const wallTop = new THREE.Mesh(new THREE.BoxGeometry(0.2, 0.8, 1.2), darkWallMat);
    wallTop.position.set(0, 3.1, 0);
    scene.add(wallTop);

    // 半开门（门洞中心，向客厅方向打开）
    const doorMat = new THREE.MeshStandardMaterial({ color: 0x8b6914, roughness: 0.6 });
    const door = new THREE.Mesh(new THREE.BoxGeometry(0.08, 2.7, 1.1), doorMat);
    door.position.set(0.05, 1.35, -0.55); // 门轴在左侧(z=-0.55)
    door.rotation.y = -Math.PI / 5;
    door.castShadow = true;
    scene.add(door);
    // 门把手
    const handle = new THREE.Mesh(new THREE.SphereGeometry(0.06, 8, 8), new THREE.MeshStandardMaterial({ color: 0xc0c0c0, metalness: 0.8 }));
    handle.position.set(0.1, 1.35, 0.45);
    scene.add(handle);

    // 踢脚线
    const baseboardMat = new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.5 });
    const bbBack = new THREE.Mesh(new THREE.BoxGeometry(14, 0.12, 0.05), baseboardMat);
    bbBack.position.set(0, 0.06, -4.88);
    scene.add(bbBack);
    const bbLeft = new THREE.Mesh(new THREE.BoxGeometry(0.05, 0.12, 10), baseboardMat);
    bbLeft.position.set(-6.88, 0.06, 0);
    scene.add(bbLeft);
    const bbRight = new THREE.Mesh(new THREE.BoxGeometry(0.05, 0.12, 10), baseboardMat);
    bbRight.position.set(6.88, 0.06, 0);
    scene.add(bbRight);

    // 顶角线（crown molding）
    const crownMat = new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.4 });
    const crownBack = new THREE.Mesh(new THREE.BoxGeometry(14, 0.15, 0.15), crownMat);
    crownBack.position.set(0, 3.35, -4.85);
    scene.add(crownBack);
    const crownLeft = new THREE.Mesh(new THREE.BoxGeometry(0.15, 0.15, 10), crownMat);
    crownLeft.position.set(-6.85, 3.35, 0);
    scene.add(crownLeft);
    const crownRight = new THREE.Mesh(new THREE.BoxGeometry(0.15, 0.15, 10), crownMat);
    crownRight.position.set(6.85, 3.35, 0);
    scene.add(crownRight);

    // 窗户（客厅左墙）
    const windowFrame = new THREE.Mesh(new THREE.BoxGeometry(0.1, 1.8, 2.5), new THREE.MeshStandardMaterial({ color: 0xffffff }));
    windowFrame.position.set(-6.85, 1.8, -2);
    scene.add(windowFrame);
    const glass = new THREE.Mesh(new THREE.PlaneGeometry(2.2, 1.5), new THREE.MeshStandardMaterial({ color: 0x87ceeb, transparent: true, opacity: 0.3, side: THREE.DoubleSide }));
    glass.rotation.y = Math.PI / 2;
    glass.position.set(-6.8, 1.8, -2);
    scene.add(glass);
    // 窗台
    const sill = new THREE.Mesh(new THREE.BoxGeometry(0.3, 0.08, 2.8), new THREE.MeshStandardMaterial({ color: 0xffffff }));
    sill.position.set(-6.75, 0.9, -2);
    scene.add(sill);

    // 窗户（卧室右墙）
    const windowFrame2 = new THREE.Mesh(new THREE.BoxGeometry(0.1, 1.8, 2), new THREE.MeshStandardMaterial({ color: 0xffffff }));
    windowFrame2.position.set(6.85, 1.8, -1.5);
    scene.add(windowFrame2);
    const glass2 = new THREE.Mesh(new THREE.PlaneGeometry(1.7, 1.5), new THREE.MeshStandardMaterial({ color: 0x87ceeb, transparent: true, opacity: 0.3, side: THREE.DoubleSide }));
    glass2.rotation.y = -Math.PI / 2;
    glass2.position.set(6.8, 1.8, -1.5);
    scene.add(glass2);
    const sill2 = new THREE.Mesh(new THREE.BoxGeometry(0.3, 0.08, 2.3), new THREE.MeshStandardMaterial({ color: 0xffffff }));
    sill2.position.set(6.75, 0.9, -1.5);
    scene.add(sill2);

    // 地毯（客厅）
    const rug = new THREE.Mesh(new THREE.PlaneGeometry(3.0, 2.2), new THREE.MeshStandardMaterial({ color: 0x8b7355, roughness: 0.9 }));
    rug.rotation.x = -Math.PI / 2;
    rug.position.set(-3.5, 0.01, -2.0);
    rug.receiveShadow = true;
    scene.add(rug);

    // 开关面板（客厅）
    const switchMat = new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.4 });
    const switch1 = new THREE.Mesh(new THREE.BoxGeometry(0.12, 0.12, 0.02), switchMat);
    switch1.position.set(-0.5, 1.4, -4.88);
    scene.add(switch1);
    const switchBtn = new THREE.Mesh(new THREE.BoxGeometry(0.04, 0.06, 0.01), new THREE.MeshStandardMaterial({ color: 0xcccccc }));
    switchBtn.position.set(-0.5, 1.4, -4.86);
    scene.add(switchBtn);

    // 插座（客厅）
    const outlet = new THREE.Mesh(new THREE.BoxGeometry(0.12, 0.15, 0.02), switchMat);
    outlet.position.set(-5.5, 0.5, -4.88);
    scene.add(outlet);

    // 挂画（客厅侧墙）
    const frame = new THREE.Mesh(new THREE.BoxGeometry(0.8, 0.6, 0.05), new THREE.MeshStandardMaterial({ color: 0x654321 }));
    frame.position.set(-6.88, 2.0, 1.5);
    frame.rotation.y = Math.PI / 2;
    scene.add(frame);
    const art = new THREE.Mesh(new THREE.PlaneGeometry(0.65, 0.45), new THREE.MeshStandardMaterial({ color: 0x4a90d9 }));
    art.position.set(-6.85, 2.0, 1.5);
    art.rotation.y = Math.PI / 2;
    scene.add(art);

    // 卧室装饰画
    const frame2 = new THREE.Mesh(new THREE.BoxGeometry(0.8, 0.6, 0.05), new THREE.MeshStandardMaterial({ color: 0x8b7355 }));
    frame2.position.set(3.5, 2.2, -4.88);
    scene.add(frame2);
    const art2 = new THREE.Mesh(new THREE.PlaneGeometry(0.65, 0.45), new THREE.MeshStandardMaterial({ color: 0xd4a574 }));
    art2.position.set(3.5, 2.2, -4.85);
    scene.add(art2);
  }

  function buildDevices() {
    // === 客厅吊灯 ===
    const lampGroup = new THREE.Group();
    const lampShade = new THREE.Mesh(new THREE.CylinderGeometry(0.3, 0.5, 0.3, 16), new THREE.MeshStandardMaterial({ color: 0xffffff, side: THREE.DoubleSide }));
    lampShade.position.y = 0;
    lampGroup.add(lampShade);
    const lampLight = new THREE.PointLight(0xfff3cd, 0, 8);
    lampLight.position.y = -0.1;
    lampGroup.add(lampLight);
    lampGroup.position.set(-3.5, 3.2, -2.0);
    scene.add(lampGroup);
    deviceMeshes['light_living_01'] = { group: lampGroup, light: lampLight, shade: lampShade };

    // === 客厅台灯（放在茶几上）===
    const deskLamp = new THREE.Group();
    const lampBase = new THREE.Mesh(new THREE.CylinderGeometry(0.12, 0.15, 0.05, 12), new THREE.MeshStandardMaterial({ color: 0x333333 }));
    deskLamp.add(lampBase);
    const lampPole = new THREE.Mesh(new THREE.CylinderGeometry(0.02, 0.02, 0.4, 8), new THREE.MeshStandardMaterial({ color: 0x333333 }));
    lampPole.position.y = 0.2;
    deskLamp.add(lampPole);
    const lampHead = new THREE.Mesh(new THREE.ConeGeometry(0.12, 0.2, 12), new THREE.MeshStandardMaterial({ color: 0xffd700, side: THREE.DoubleSide }));
    lampHead.position.y = 0.45;
    lampHead.rotation.x = Math.PI;
    deskLamp.add(lampHead);
    const deskLight = new THREE.PointLight(0xfff3cd, 0, 4);
    deskLight.position.y = 0.35;
    deskLamp.add(deskLight);
    deskLamp.position.set(-3.5, 0.45, -2.2);
    scene.add(deskLamp);
    deviceMeshes['light_living_02'] = { group: deskLamp, light: deskLight, shade: lampHead };

    // === 窗帘（客厅窗户）===
    const curtainMat = new THREE.MeshStandardMaterial({ color: 0x9b8bb5, side: THREE.DoubleSide });
    const curtainLeft = new THREE.Mesh(new THREE.PlaneGeometry(1.2, 2.5), curtainMat);
    curtainLeft.rotation.y = Math.PI / 2;
    curtainLeft.position.set(-6.75, 1.5, -2.8);
    scene.add(curtainLeft);
    const curtainRight = new THREE.Mesh(new THREE.PlaneGeometry(1.2, 2.5), curtainMat);
    curtainRight.rotation.y = Math.PI / 2;
    curtainRight.position.set(-6.75, 1.5, -1.2);
    scene.add(curtainRight);
    deviceMeshes['curtain_living_01'] = { left: curtainLeft, right: curtainRight, baseZ: -2.0 };

    // === 空调（客厅后墙）===
    const acGroup = new THREE.Group();
    const acBody = new THREE.Mesh(new THREE.BoxGeometry(1.5, 0.35, 0.25), new THREE.MeshStandardMaterial({ color: 0xf5f5f5 }));
    acGroup.add(acBody);
    const acVent = new THREE.Mesh(new THREE.BoxGeometry(1.3, 0.08, 0.05), new THREE.MeshStandardMaterial({ color: 0xcccccc }));
    acVent.position.set(0, -0.1, 0.13);
    acGroup.add(acVent);
    const acLed = new THREE.Mesh(new THREE.BoxGeometry(0.15, 0.04, 0.02), new THREE.MeshStandardMaterial({ color: 0x333333, emissive: 0x000000 }));
    acLed.position.set(0.5, 0.05, 0.13);
    acGroup.add(acLed);
    acGroup.position.set(-5.8, 2.8, -4.85);
    scene.add(acGroup);
    deviceMeshes['ac_living_01'] = { group: acGroup, led: acLed, vent: acVent, particles: [] };

    // === 电视（放在电视柜上）===
    const tvGroup = new THREE.Group();
    const tvBody = new THREE.Mesh(new THREE.BoxGeometry(1.8, 1.0, 0.08), new THREE.MeshStandardMaterial({ color: 0x111111 }));
    tvGroup.add(tvBody);
    const tvScreen = new THREE.Mesh(new THREE.PlaneGeometry(1.65, 0.85), new THREE.MeshStandardMaterial({ color: 0x000000, emissive: 0x000000 }));
    tvScreen.position.z = 0.05;
    tvGroup.add(tvScreen);
    const tvStand = new THREE.Mesh(new THREE.BoxGeometry(0.4, 0.15, 0.2), new THREE.MeshStandardMaterial({ color: 0x222222 }));
    tvStand.position.y = -0.55;
    tvGroup.add(tvStand);
    tvGroup.position.set(-3.5, 0.85, -4.1);
    scene.add(tvGroup);
    deviceMeshes['tv_living_01'] = { group: tvGroup, screen: tvScreen };

    // === 风扇（客厅角落）===
    const fanGroup = new THREE.Group();
    const fanBase = new THREE.Mesh(new THREE.CylinderGeometry(0.2, 0.25, 0.05, 12), new THREE.MeshStandardMaterial({ color: 0x444444 }));
    fanGroup.add(fanBase);
    const fanPole = new THREE.Mesh(new THREE.CylinderGeometry(0.03, 0.03, 1.0, 8), new THREE.MeshStandardMaterial({ color: 0x444444 }));
    fanPole.position.y = 0.5;
    fanGroup.add(fanPole);
    const fanHead = new THREE.Group();
    fanHead.position.y = 1.1;
    const fanCage = new THREE.Mesh(new THREE.TorusGeometry(0.25, 0.02, 8, 24), new THREE.MeshStandardMaterial({ color: 0x666666 }));
    fanHead.add(fanCage);
    const fanBlades = new THREE.Group();
    for (let i = 0; i < 3; i++) {
      const blade = new THREE.Mesh(new THREE.BoxGeometry(0.04, 0.2, 0.01), new THREE.MeshStandardMaterial({ color: 0x888888 }));
      blade.position.y = 0.1;
      blade.rotation.z = (i * Math.PI * 2) / 3;
      fanBlades.add(blade);
    }
    fanHead.add(fanBlades);
    fanGroup.add(fanHead);
    fanGroup.position.set(-6, 0, 3);
    scene.add(fanGroup);
    deviceMeshes['fan_living_01'] = { group: fanGroup, blades: fanBlades };

    // === 加湿器（客厅角落）===
    const humGroup = new THREE.Group();
    const humBody = new THREE.Mesh(new THREE.CylinderGeometry(0.15, 0.18, 0.4, 16), new THREE.MeshStandardMaterial({ color: 0x87ceeb, transparent: true, opacity: 0.7 }));
    humGroup.add(humBody);
    const humTop = new THREE.Mesh(new THREE.CylinderGeometry(0.08, 0.12, 0.1, 12), new THREE.MeshStandardMaterial({ color: 0xffffff }));
    humTop.position.y = 0.25;
    humGroup.add(humTop);
    humGroup.position.set(-5.5, 0.2, 3);
    scene.add(humGroup);
    deviceMeshes['humidifier_living_01'] = { group: humGroup, particles: [] };

    // === 卧室吸顶灯 ===
    const bedLamp = new THREE.Group();
    const bedLampShade = new THREE.Mesh(new THREE.CylinderGeometry(0.25, 0.35, 0.15, 16), new THREE.MeshStandardMaterial({ color: 0xffffff, side: THREE.DoubleSide }));
    bedLamp.add(bedLampShade);
    const bedLampLight = new THREE.PointLight(0xfff3cd, 0, 6);
    bedLampLight.position.y = -0.05;
    bedLamp.add(bedLampLight);
    bedLamp.position.set(3.5, 3.2, -1.5);
    scene.add(bedLamp);
    deviceMeshes['light_bedroom_01'] = { group: bedLamp, light: bedLampLight, shade: bedLampShade };

    // === 卧室窗帘 ===
    const bedCurtainL = new THREE.Mesh(new THREE.PlaneGeometry(1, 2.2), new THREE.MeshStandardMaterial({ color: 0xb5a5c8, side: THREE.DoubleSide }));
    bedCurtainL.rotation.y = -Math.PI / 2;
    bedCurtainL.position.set(6.75, 1.4, -2.3);
    scene.add(bedCurtainL);
    const bedCurtainR = new THREE.Mesh(new THREE.PlaneGeometry(1, 2.2), new THREE.MeshStandardMaterial({ color: 0xb5a5c8, side: THREE.DoubleSide }));
    bedCurtainR.rotation.y = -Math.PI / 2;
    bedCurtainR.position.set(6.75, 1.4, -0.7);
    scene.add(bedCurtainR);
    deviceMeshes['curtain_bedroom_01'] = { left: bedCurtainL, right: bedCurtainR, baseZ: -1.5 };

    // === 卧室空调 ===
    const bedAc = acGroup.clone();
    bedAc.position.set(5.5, 2.8, -4.85);
    scene.add(bedAc);
    deviceMeshes['ac_bedroom_01'] = { group: bedAc, led: bedAc.children[2], vent: bedAc.children[1], particles: [] };

    // === 温湿度计（客厅隔墙面上）===
    const thermo = new THREE.Group();
    const thermoBody = new THREE.Mesh(new THREE.BoxGeometry(0.05, 0.4, 0.3), new THREE.MeshStandardMaterial({ color: 0xffffff }));
    thermo.add(thermoBody);
    const thermoScreen = new THREE.Mesh(new THREE.PlaneGeometry(0.22, 0.25), new THREE.MeshStandardMaterial({ color: 0x1a1a2e, emissive: 0x00ff00 }));
    thermoScreen.rotation.y = Math.PI / 2;
    thermoScreen.position.x = 0.03;
    thermo.add(thermoScreen);
    thermo.position.set(-0.12, 2.0, -3.5);
    scene.add(thermo);

    // === 人体红外传感器（客厅墙角）===
    const pir = new THREE.Mesh(new THREE.SphereGeometry(0.08, 8, 8), new THREE.MeshStandardMaterial({ color: 0xff0000, emissive: 0x330000 }));
    pir.position.set(-6.8, 2.5, -4.5);
    scene.add(pir);
    deviceMeshes['pir_living'] = { mesh: pir };

    // === 人体红外传感器（卧室墙角）===
    const pir2 = pir.clone();
    pir2.position.set(6.8, 2.5, -4.5);
    scene.add(pir2);
    deviceMeshes['pir_bedroom'] = { mesh: pir2 };

    // === 装饰性落地灯光源（常亮，增强氛围）===
    const floorLampLight = new THREE.PointLight(0xffeedd, 0.6, 5);
    floorLampLight.position.set(-5.8, 1.5, -0.5);
    scene.add(floorLampLight);
  }

  function initOrbitControls(container) {
    // 自实现轨道控制：左键旋转、中键(滚轮)平移、滚轮缩放
    let isRotating = false;
    let isPanning = false;
    let prevX = 0, prevY = 0;
    let theta = 0;
    let phi = Math.PI / 4;
    let radius = 13;
    const target = new THREE.Vector3(0, 0.5, -1);

    function updateCamera() {
      camera.position.x = target.x + radius * Math.sin(phi) * Math.sin(theta);
      camera.position.y = target.y + radius * Math.cos(phi);
      camera.position.z = target.z + radius * Math.sin(phi) * Math.cos(theta);
      camera.lookAt(target);
    }
    updateCamera();

    container.addEventListener('mousedown', (e) => {
      prevX = e.clientX;
      prevY = e.clientY;
      if (e.button === 0) {
        isRotating = true;
      } else if (e.button === 1) {
        isPanning = true;
        e.preventDefault();
      }
    });
    window.addEventListener('mouseup', () => { isRotating = false; isPanning = false; });
    window.addEventListener('mousemove', (e) => {
      const dx = e.clientX - prevX;
      const dy = e.clientY - prevY;
      prevX = e.clientX;
      prevY = e.clientY;

      if (isRotating) {
        theta -= dx * 0.008;
        phi = Math.max(0.2, Math.min(Math.PI / 2.1, phi - dy * 0.008));
        updateCamera();
      } else if (isPanning) {
        // 平移：沿相机 right 和 up 方向移动 target
        const panSpeed = radius * 0.0015;
        const right = new THREE.Vector3();
        const up = new THREE.Vector3(0, 1, 0);
        camera.getWorldDirection(right);
        right.cross(up).normalize();
        target.addScaledVector(right, -dx * panSpeed);
        target.y += dy * panSpeed;
        target.x = Math.max(-8, Math.min(8, target.x));
        target.y = Math.max(0, Math.min(5, target.y));
        target.z = Math.max(-6, Math.min(6, target.z));
        updateCamera();
      }
    });
    container.addEventListener('wheel', (e) => {
      e.preventDefault();
      radius = Math.max(5, Math.min(25, radius + e.deltaY * 0.01));
      updateCamera();
    }, { passive: false });

    // 触摸支持：单指旋转，双指缩放
    let lastTouchDist = 0;
    container.addEventListener('touchstart', (e) => {
      if (e.touches.length === 1) {
        isRotating = true;
        prevX = e.touches[0].clientX;
        prevY = e.touches[0].clientY;
      } else if (e.touches.length === 2) {
        isPanning = true;
        const dx = e.touches[0].clientX - e.touches[1].clientX;
        const dy = e.touches[0].clientY - e.touches[1].clientY;
        lastTouchDist = Math.sqrt(dx*dx + dy*dy);
      }
    });
    container.addEventListener('touchend', () => { isRotating = false; isPanning = false; });
    container.addEventListener('touchmove', (e) => {
      e.preventDefault();
      if (isRotating && e.touches.length === 1) {
        const dx = e.touches[0].clientX - prevX;
        const dy = e.touches[0].clientY - prevY;
        theta -= dx * 0.008;
        phi = Math.max(0.2, Math.min(Math.PI / 2.1, phi - dy * 0.008));
        prevX = e.touches[0].clientX;
        prevY = e.touches[0].clientY;
        updateCamera();
      } else if (isPanning && e.touches.length === 2) {
        const dx = e.touches[0].clientX - e.touches[1].clientX;
        const dy = e.touches[0].clientY - e.touches[1].clientY;
        const dist = Math.sqrt(dx*dx + dy*dy);
        if (lastTouchDist > 0) {
          radius = Math.max(5, Math.min(25, radius - (dist - lastTouchDist) * 0.02));
          updateCamera();
        }
        lastTouchDist = dist;
      }
    }, { passive: false });
  }

  function updateDevice(device) {
    const id = device.device_id;
    const mesh = deviceMeshes[id];
    if (!mesh) return;
    const a = device.attributes || {};
    const on = a.on ? a.on.value : false;

    // 设备离线/故障：整体变灰
    if (device.state && device.state !== 'online') {
      const isFault = device.state === 'fault';
      const targetColor = isFault ? 0xff6666 : 0x666666;
      if (mesh.group) {
        mesh.group.traverse((child) => {
          if (child.isMesh && child.material) {
            if (!child.userData.origColor) {
              child.userData.origColor = child.material.color.getHex();
            }
            child.material.color.setHex(targetColor);
            child.material.opacity = 0.6;
            child.material.transparent = true;
          }
        });
      }
      if (mesh.light) mesh.light.intensity = 0;
      return;
    } else if (mesh.group) {
      // 恢复在线状态的原始颜色
      mesh.group.traverse((child) => {
        if (child.isMesh && child.material && child.userData.origColor) {
          child.material.color.setHex(child.userData.origColor);
          child.material.opacity = 1;
          child.material.transparent = false;
        }
      });
    }

    if (device.category === 'light') {
      const brightness = on ? (a.brightness ? a.brightness.value : 80) : 0;
      if (mesh.light) mesh.light.intensity = brightness / 100 * 2;
      if (mesh.shade) mesh.shade.material.emissive = new THREE.Color(on ? 0xfff3cd : 0x000000);
      if (mesh.shade) mesh.shade.material.emissiveIntensity = on ? 0.5 : 0;
    } else if (device.category === 'curtain') {
      const pos = a.position ? a.position.value : 0;
      const offset = (pos / 100) * 1.0;
      // 窗帘在侧墙上，沿 z 轴方向开合（向窗户两侧收拢）
      if (mesh.left) mesh.left.position.z = mesh.baseZ - offset;
      if (mesh.right) mesh.right.position.z = mesh.baseZ + offset;
    } else if (device.category === 'ac') {
      if (mesh.led) {
        mesh.led.material.emissive = new THREE.Color(on ? 0x00ff00 : 0x000000);
        mesh.led.material.emissiveIntensity = on ? 1 : 0;
      }
      // 出风粒子（向前吹出，模拟冷风）
      if (on && mesh.particles.length < 10) {
        for (let i = mesh.particles.length; i < 10; i++) {
          const p = new THREE.Mesh(new THREE.SphereGeometry(0.025, 6, 6), new THREE.MeshBasicMaterial({ color: 0xaaddff, transparent: true, opacity: 0.5 }));
          p.position.copy(mesh.group.position);
          p.position.y -= 0.15; // 出风口
          p.position.z += 0.15;
          p.position.x += (Math.random() - 0.5) * 1.0; // 沿空调宽度分布
          p.userData.velocity = { x: (Math.random()-0.5)*0.01, y: -0.003 - Math.random()*0.003, z: 0.06 + Math.random()*0.03 };
          p.userData.type = 'ac';
          p.userData.originZ = mesh.group.position.z + 0.15;
          scene.add(p);
          mesh.particles.push(p);
        }
      } else if (!on) {
        mesh.particles.forEach(p => scene.remove(p));
        mesh.particles = [];
      }
    } else if (device.category === 'tv') {
      if (mesh.screen) {
        mesh.screen.material.emissive = new THREE.Color(on ? 0x4488ff : 0x000000);
        mesh.screen.material.emissiveIntensity = on ? 0.8 : 0;
        mesh.screen.material.color = new THREE.Color(on ? 0x222222 : 0x000000);
      }
    } else if (device.category === 'fan') {
      mesh.group.visible = on;
    } else if (device.category === 'humidifier') {
      if (on && mesh.particles.length < 12) {
        for (let i = mesh.particles.length; i < 12; i++) {
          const p = new THREE.Mesh(new THREE.SphereGeometry(0.025, 6, 6), new THREE.MeshBasicMaterial({ color: 0xaaddff, transparent: true, opacity: 0.5 }));
          p.position.copy(mesh.group.position);
          p.position.y += 0.4;
          p.position.x += (Math.random() - 0.5) * 0.1;
          p.userData.velocity = { x: (Math.random()-0.5)*0.008, y: 0.02 + Math.random()*0.015, z: (Math.random()-0.5)*0.008 };
          p.userData.type = 'humidifier';
          scene.add(p);
          mesh.particles.push(p);
        }
      } else if (!on) {
        mesh.particles.forEach(p => scene.remove(p));
        mesh.particles = [];
      }
    }
  }

  function setEnvironment(sensors) {
    environment = { ...environment, ...sensors };
    if (scene.userData.ambient) {
      const lightFactor = Math.min(1, (sensors.light_level || 400) / 800);
      scene.userData.ambient.intensity = 0.15 + lightFactor * 0.45;
      // 昼夜背景色变化
      if (lightFactor < 0.3) {
        scene.background = new THREE.Color(0x0a0a1a); // 深夜
      } else if (lightFactor < 0.6) {
        scene.background = new THREE.Color(0x1a1a2e); // 黄昏/清晨
      } else {
        scene.background = new THREE.Color(0x2a2a3e); // 白天
      }
      // 方向光强度随时间变化
      if (scene.userData.dirLight) {
        scene.userData.dirLight.intensity = 0.3 + lightFactor * 0.7;
      }
    }
    // 人体红外传感器状态
    if (deviceMeshes['pir_living'] && deviceMeshes['pir_living'].mesh) {
      deviceMeshes['pir_living'].mesh.material.emissive = new THREE.Color(sensors.motion_living ? 0xff0000 : 0x330000);
      deviceMeshes['pir_living'].mesh.material.emissiveIntensity = sensors.motion_living ? 1 : 0.3;
    }
    if (deviceMeshes['pir_bedroom'] && deviceMeshes['pir_bedroom'].mesh) {
      deviceMeshes['pir_bedroom'].mesh.material.emissive = new THREE.Color(sensors.motion_bedroom ? 0xff0000 : 0x330000);
      deviceMeshes['pir_bedroom'].mesh.material.emissiveIntensity = sensors.motion_bedroom ? 1 : 0.3;
    }
  }

  function animate() {
    requestAnimationFrame(animate);

    // 风扇旋转
    if (deviceMeshes['fan_living_01'] && deviceMeshes['fan_living_01'].blades) {
      deviceMeshes['fan_living_01'].blades.rotation.z += 0.15;
    }

    // 空调出风 + 加湿器水雾粒子（统一用 velocity）
    Object.values(deviceMeshes).forEach((m) => {
      if (m.particles && m.particles.length > 0) {
        m.particles.forEach((p) => {
          const v = p.userData.velocity;
          if (!v) return;
          p.position.x += v.x;
          p.position.y += v.y;
          p.position.z += v.z;
          if (p.userData.type === 'ac') {
            // 空调冷风：向前吹，逐渐扩散变淡，吹到一定距离重置
            p.material.opacity = Math.max(0, p.material.opacity - 0.008);
            if (p.position.z > p.userData.originZ + 2.5 || p.material.opacity <= 0) {
              p.position.copy(m.group.position);
              p.position.y -= 0.15;
              p.position.z += 0.15;
              p.position.x += (Math.random() - 0.5) * 1.0;
              p.material.opacity = 0.5;
            }
          } else if (p.userData.type === 'humidifier') {
            // 加湿器：上升扩散
            p.material.opacity = Math.max(0, p.material.opacity - 0.006);
            if (p.position.y > 1.8 || p.material.opacity <= 0) {
              p.position.copy(m.group.position);
              p.position.y += 0.4;
              p.position.x += (Math.random() - 0.5) * 0.1;
              p.material.opacity = 0.5;
            }
          }
        });
      }
    });

    // 电视屏幕动态画面
    if (deviceMeshes['tv_living_01'] && deviceMeshes['tv_living_01'].screen) {
      const tv = deviceMeshes['tv_living_01'];
      if (tv.screen.material.emissiveIntensity > 0) {
        const hue = (Date.now() / 50) % 360;
        tv.screen.material.emissive.setHSL(hue / 360, 0.6, 0.4);
      }
    }

    renderer.render(scene, camera);
  }

  return { init, updateDevice, setEnvironment };
})();

export default HomeScene;
