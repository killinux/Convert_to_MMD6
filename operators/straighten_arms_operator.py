"""
拉直手臂（肘 + 腕）操作符

通用功能：对任意已在 UI 中映射了手臂骨骼的模型（XPS / XNALara 等），
自动检测两个环节的弯曲并修正——
  1. 大臂 -> 小臂（肘 / ひじ）
  2. 小臂 -> 手 （腕 / 手首）
若某环节的夹角大于阈值，则在姿态模式下把子骨旋转到与父骨共线，
随后把形变烘焙进网格（网格真正跟随，而不仅仅是骨骼移动），
最后将该姿态固化为新的静置姿态(rest pose)。

关键点：只做 `armature_apply` 会让骨骼变直但网格弹回原状（骨皮错位）。
正确做法必须先把当前姿态烘焙进网格顶点，再 `armature_apply`。
"""
import bpy
import math
from mathutils import Vector


def _iter_armature_meshes(arm):
    """返回所有以该骨架为目标、带骨架修改器的网格对象。"""
    result = []
    for mo in bpy.data.objects:
        if mo.type != 'MESH':
            continue
        if any(md.type == 'ARMATURE' and md.object == arm for md in mo.modifiers):
            result.append(mo)
    return result


def _backup_shape_keys(mesh_obj):
    """备份并移除网格的形态键（modifier_apply 不能作用于带形态键的网格）。"""
    backup = []
    sk = mesh_obj.data.shape_keys
    if sk and sk.key_blocks:
        basis = sk.key_blocks[0]
        for kb in sk.key_blocks[1:]:
            deltas = {i: (v.co - basis.data[i].co)
                      for i, v in enumerate(kb.data) if v.co != basis.data[i].co}
            backup.append({
                'name': kb.name, 'deltas': deltas,
                'slider_min': kb.slider_min, 'slider_max': kb.slider_max,
                'mute': kb.mute, 'value': kb.value,
                'relative_key': kb.relative_key.name if kb.relative_key else None,
            })
        for kb in reversed(list(sk.key_blocks)):
            mesh_obj.shape_key_remove(kb)
    return backup


def _restore_shape_keys(mesh_obj, backup):
    """在烘焙后的基础形状上恢复形态键。"""
    if not backup:
        return
    mesh_obj.shape_key_add(from_mix=False)  # basis
    basis = mesh_obj.data.shape_keys.key_blocks[0]
    for item in backup:
        kb = mesh_obj.shape_key_add(name=item['name'])
        for i, d in item['deltas'].items():
            kb.data[i].co = basis.data[i].co + d
        kb.slider_min = item['slider_min']
        kb.slider_max = item['slider_max']
        kb.mute = item['mute']
        kb.value = item['value']
        if item['relative_key'] and item['relative_key'] in mesh_obj.data.shape_keys.key_blocks:
            kb.relative_key = mesh_obj.data.shape_keys.key_blocks[item['relative_key']]


def _bone_angle(arm, parent_name, child_name):
    """父骨方向与子骨方向的夹角（度）。骨骼不存在返回 None。"""
    pb = arm.data.bones.get(parent_name)
    cb = arm.data.bones.get(child_name)
    if not pb or not cb:
        return None
    vp = (pb.tail_local - pb.head_local)
    vc = (cb.tail_local - cb.head_local)
    if vp.length == 0 or vc.length == 0:
        return None
    return math.degrees(vp.angle(vc))


def _align_child_to_parent(arm, parent_name, child_name):
    """姿态模式下：把子骨方向旋转到与父骨方向共线，绕子骨头部(关节)旋转。"""
    pp = arm.pose.bones.get(parent_name)
    pc = arm.pose.bones.get(child_name)
    if not pp or not pc:
        return None
    d_parent = pp.vector.normalized()
    d_child = pc.vector.normalized()
    before = math.degrees(d_child.angle(d_parent))
    rot = d_child.rotation_difference(d_parent).to_matrix().to_4x4()
    mat = pc.matrix.copy()
    head_loc = mat.to_translation()
    new_mat = rot @ mat
    new_mat.translation = head_loc  # 关节位置不变，只改朝向
    pc.matrix = new_mat
    bpy.context.view_layer.update()
    return before


class OBJECT_OT_straighten_arms(bpy.types.Operator):
    """检测并修正手臂肘/腕弯曲，使大臂→小臂→手共线（网格烘焙跟随）"""
    bl_idname = "object.straighten_arms"
    bl_label = "拉直手臂(肘+腕)"
    bl_description = ("通用：检测大臂→小臂(肘)与小臂→手(腕)两个环节，"
                     "夹角超过阈值就拉直成共线，网格跟随烘焙。需先在上方映射手臂骨骼")
    bl_options = {'REGISTER', 'UNDO'}

    angle_threshold: bpy.props.FloatProperty(  # type: ignore
        name="阈值(度)", description="夹角大于此值才修正", default=0.5, min=0.0, max=45.0)
    fix_wrist: bpy.props.BoolProperty(  # type: ignore
        name="同时拉直手腕", description="除肘部外，也把小臂→手拉直", default=True)
    check_only: bpy.props.BoolProperty(  # type: ignore
        name="仅检测不修改", description="只报告当前角度，不做任何修改", default=False)

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'ARMATURE':
            self.report({'ERROR'}, "请先选择骨架对象")
            return {'CANCELLED'}
        scene = context.scene

        # 从映射属性读取两侧手臂骨骼（支持原始名或已重命名为MMD名）
        sides = {
            'left': (getattr(scene, "left_upper_arm_bone", ""),
                     getattr(scene, "left_lower_arm_bone", ""),
                     getattr(scene, "left_hand_bone", "")),
            'right': (getattr(scene, "right_upper_arm_bone", ""),
                      getattr(scene, "right_lower_arm_bone", ""),
                      getattr(scene, "right_hand_bone", "")),
        }
        if not any(u and l for (u, l, h) in sides.values()):
            self.report({'ERROR'}, "未找到手臂骨骼映射，请先在面板中设置上臂/下臂(/手)骨骼")
            return {'CANCELLED'}

        # 1) 收集需要修正的环节
        jobs = []          # (parent_name, child_name, label)
        report_lines = []
        for side, (upper, lower, hand) in sides.items():
            if upper and lower:
                a = _bone_angle(obj, upper, lower)
                if a is not None:
                    report_lines.append(f"{side} 肘 {a:.2f}°")
                    if a > self.angle_threshold:
                        jobs.append((upper, lower, f"{side}-肘"))
            if self.fix_wrist and lower and hand:
                a = _bone_angle(obj, lower, hand)
                if a is not None:
                    report_lines.append(f"{side} 腕 {a:.2f}°")
                    if a > self.angle_threshold:
                        jobs.append((lower, hand, f"{side}-腕"))

        if self.check_only:
            self.report({'INFO'}, "检测结果: " + ", ".join(report_lines) +
                        (f" | 需修正: {len(jobs)} 处" if jobs else " | 全部已伸直"))
            return {'FINISHED'}

        if not jobs:
            self.report({'INFO'}, "手臂已伸直，无需修正: " + ", ".join(report_lines))
            return {'FINISHED'}

        # 2) 进入姿态模式，按 肘 -> 腕 顺序拉直（腕对齐到已拉直的小臂）
        if context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        context.view_layer.objects.active = obj
        bpy.ops.object.mode_set(mode='POSE')

        fixed = []
        # 先处理所有"肘"，再处理所有"腕"，保证腕基于拉直后的小臂
        for parent, child, label in sorted(jobs, key=lambda j: 0 if j[2].endswith("肘") else 1):
            before = _align_child_to_parent(obj, parent, child)
            if before is not None:
                fixed.append(f"{label}({before:.1f}°)")

        # 3) 把当前姿态烘焙进网格（关键：网格跟随），再固化为静置姿态
        meshes = _iter_armature_meshes(obj)
        specs = {}
        bpy.ops.object.mode_set(mode='OBJECT')
        for mo in meshes:
            sk_backup = _backup_shape_keys(mo)
            md = next(m for m in mo.modifiers if m.type == 'ARMATURE' and m.object == obj)
            specs[mo.name] = (md.name, md.use_vertex_groups, md.use_bone_envelopes,
                              md.use_deform_preserve_volume, sk_backup)
            bpy.ops.object.select_all(action='DESELECT')
            mo.select_set(True)
            context.view_layer.objects.active = mo
            bpy.ops.object.modifier_apply(modifier=md.name)

        # 固化姿态为新的 rest pose
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        context.view_layer.objects.active = obj
        bpy.ops.object.mode_set(mode='POSE')
        bpy.ops.pose.armature_apply()
        bpy.ops.object.mode_set(mode='OBJECT')

        # 重新挂回骨架修改器并恢复形态键
        for mo in meshes:
            name, uvg, ube, udpv, sk_backup = specs[mo.name]
            nm = mo.modifiers.new(name=name, type='ARMATURE')
            nm.object = obj
            nm.use_vertex_groups = uvg
            nm.use_bone_envelopes = ube
            nm.use_deform_preserve_volume = udpv
            _restore_shape_keys(mo, sk_backup)

        # 选回骨架
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        context.view_layer.objects.active = obj

        self.report({'INFO'}, f"已拉直 {len(fixed)} 处: " + ", ".join(fixed))
        return {'FINISHED'}


def register():
    bpy.utils.register_class(OBJECT_OT_straighten_arms)


def unregister():
    bpy.utils.unregister_class(OBJECT_OT_straighten_arms)


if __name__ == "__main__":
    register()
