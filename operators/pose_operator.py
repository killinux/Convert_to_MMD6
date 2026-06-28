import bpy
import math
from mathutils import Matrix
from ..bone_utils import apply_armature_transforms
# 新增的T-Pose到A-Pose转换操作符
class OBJECT_OT_convert_to_apose(bpy.types.Operator):
    """将骨架转换为 A-Pose 并应用为新的静置姿态"""
    bl_idname = "object.convert_to_apose" 
    bl_label = "Convert to A-Pose"

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'ARMATURE':
            self.report({'ERROR'}, "未选择骨架对象")
            return {'CANCELLED'} 
        if not apply_armature_transforms(context, obj):
            self.report({'ERROR'}, "应用骨架变换失败")
            return {'CANCELLED'}
        scene = context.scene
        
        # 获取骨骼名称
        arm_bones = {
            "left_upper_arm": getattr(scene, "left_upper_arm_bone", ""),
            "right_upper_arm": getattr(scene, "right_upper_arm_bone", ""),
            "left_lower_arm": getattr(scene, "left_lower_arm_bone", ""),
            "right_lower_arm": getattr(scene, "right_lower_arm_bone", ""),          
        }

        # 检查是否有设置骨骼
        if not any(arm_bones.values()):
            self.report({'ERROR'}, "请先在UI中设置要转换的骨骼")
            return {'CANCELLED'}
        #作用：备份所有网格的形态键，包括基础键
        def backup_shape_keys(mesh_obj):
            backup = {}
            if mesh_obj.data.shape_keys and mesh_obj.data.shape_keys.key_blocks:
                basis = mesh_obj.data.shape_keys.key_blocks[0]
                for key_block in mesh_obj.data.shape_keys.key_blocks[1:]:
                    affected = {}
                    for i, v in enumerate(key_block.data):
                        if v.co != basis.data[i].co:
                            affected[i] = v.co - basis.data[i].co
                    backup[key_block.name] = {
                        'affected': affected,
                        'relative_key': key_block.relative_key.name if key_block.relative_key else None,
                        'slider_min': key_block.slider_min,
                        'slider_max': key_block.slider_max,
                        'mute': key_block.mute,
                        'value': key_block.value
                    }
            if mesh_obj.data.shape_keys:
                keys_to_remove = list(mesh_obj.data.shape_keys.key_blocks)
                for key_block in reversed(keys_to_remove):
                    mesh_obj.shape_key_remove(key_block)
            return backup

        def restore_shape_keys(mesh_obj, backup):
            if not backup:
                return
            context.view_layer.objects.active = mesh_obj
            bpy.ops.object.shape_key_add(from_mix=False)
            new_basis = mesh_obj.data.shape_keys.key_blocks[0]
            for key_name, key_data in backup.items():
                new_key = mesh_obj.shape_key_add(name=key_name)
                for i, delta in key_data['affected'].items():
                    new_key.data[i].co = new_basis.data[i].co + delta
                new_key.slider_min = key_data['slider_min']
                new_key.slider_max = key_data['slider_max']
                new_key.mute = key_data['mute']
                new_key.value = key_data['value']
                if key_data['relative_key'] and key_data['relative_key'] in mesh_obj.data.shape_keys.key_blocks:
                    new_key.relative_key = mesh_obj.data.shape_keys.key_blocks[key_data['relative_key']]

        meshes_with_armature = []
        shape_key_backups = {}
        for mesh_obj in bpy.data.objects:
            if mesh_obj.type == 'MESH':
                for modifier in mesh_obj.modifiers:
                    if modifier.type == 'ARMATURE' and modifier.object == obj:
                        shape_key_backups[mesh_obj.name] = backup_shape_keys(mesh_obj)
                        meshes_with_armature.append(mesh_obj)
                        break

        if not meshes_with_armature:
            self.report({'WARNING'}, "未找到有上臂权重的网格，跳过网格姿态调整")

        # 切换到编辑模式，将upper_arm的尾部连接到lowerarm的头部
        bpy.ops.object.mode_set(mode='EDIT')
        edit_bones = obj.data.edit_bones
        
        # 获取骨骼位置
        left_upper_arm = edit_bones[arm_bones["left_upper_arm"]]
        left_lower_arm = edit_bones[arm_bones["left_lower_arm"]]
        right_upper_arm = edit_bones[arm_bones["right_upper_arm"]]
        right_lower_arm = edit_bones[arm_bones["right_lower_arm"]]
        
        # 调整尾部位置
        left_upper_arm.tail = left_lower_arm.head
        right_upper_arm.tail = right_lower_arm.head

        # 5. 为每个网格复制骨骼修改器，但保留原始修改器
        for mesh_obj in meshes_with_armature:
            for modifier in mesh_obj.modifiers:
                if modifier.type == 'ARMATURE' and modifier.object == obj:
                    # 复制修改器
                    new_modifier = mesh_obj.modifiers.new(name=modifier.name + "_copy", type='ARMATURE')
                    new_modifier.object = modifier.object
                    new_modifier.use_vertex_groups = modifier.use_vertex_groups
                    new_modifier.use_bone_envelopes = modifier.use_bone_envelopes
                    break

        # 6. 切换到姿态模式设置A-Pose
        context.view_layer.objects.active = obj
        bpy.ops.object.mode_set(mode='POSE')

        # 7. 清除所有现有姿态
        bpy.ops.pose.select_all(action='SELECT')
        bpy.ops.pose.rot_clear()
        bpy.ops.pose.scale_clear()
        bpy.ops.pose.loc_clear()
        bpy.ops.pose.select_all(action='DESELECT')

        # 8. 为骨骼设置A-Pose旋转
        pose_bones = obj.pose.bones
        edit_bones = obj.data.bones
        converted_bones = []
        target_angle = 36  # 目标角度36度

        for bone_type, bone_name in arm_bones.items():
            if bone_name and bone_name in pose_bones and bone_name in edit_bones:
                bone = pose_bones[bone_name]
                edit_bone = edit_bones[bone_name]
                bone.rotation_mode = 'XYZ'
                
                # 获取骨骼的头部和尾部坐标
                head = edit_bone.head_local
                tail = edit_bone.tail_local
                
                # 计算方向向量（从尾指向头）
                vec = head - tail
                
                # 使用四元数转换得到欧拉角
                quat = vec.to_track_quat('Z', 'Y')
                euler = quat.to_euler('XYZ')
                
                # 获取当前X轴旋转角度
                current_angle = math.degrees(euler.x)
                
                # 计算角度差（左右两侧使用相同的计算逻辑）
                angle_diff = current_angle - target_angle
                
                # 重置旋转，确保从默认状态开始
                bone.rotation_euler = (0, 0, 0)
                
                # 选择骨骼（姿态模式下选择走底层 Bone.select，PoseBone 无 select 属性）
                bpy.ops.pose.select_all(action='DESELECT')
                bone.bone.select = True
                obj.data.bones.active = bone.bone
                
                # 绕全局空间 Y 轴旋转
                if bone_type == "left_upper_arm":
                    # 左上肢：绕全局 Y 轴旋转 angle_diff 角度
                    bpy.ops.transform.rotate(value=math.radians(-angle_diff), orient_axis='Y', orient_type='GLOBAL')
                elif bone_type == "right_upper_arm":
                    # 右上肢：绕全局 Y 轴旋转 -angle_diff 角度
                    bpy.ops.transform.rotate(value=math.radians(angle_diff), orient_axis='Y', orient_type='GLOBAL')
                
                converted_bones.append(bone_name)

        if not converted_bones:
            self.report({'WARNING'}, "没有找到匹配的骨骼可以转换")
            return {'CANCELLED'}

        # 9. 更新视图以确保姿态已应用
        context.view_layer.update()

        # 9.5 自动修正肘部弯曲：让小臂(下臂)方向与大臂(上臂)共线，手部作为子级自动跟随
        #     通用于任意源骨骼——只要在UI里映射了上臂和下臂；无弯曲则跳过
        def _straighten_elbow(upper_name, lower_name):
            if not upper_name or not lower_name:
                return None
            pu = pose_bones.get(upper_name)
            pl = pose_bones.get(lower_name)
            if not pu or not pl:
                return None
            d_upper = pu.vector.normalized()
            d_lower = pl.vector.normalized()
            angle_before = math.degrees(d_lower.angle(d_upper))
            if angle_before < 0.5:
                return None  # 已基本伸直，无需修正
            # 计算把小臂方向旋到大臂方向的旋转，绕肘关节(下臂头部)施加
            rot = d_lower.rotation_difference(d_upper).to_matrix().to_4x4()
            mat = pl.matrix.copy()
            head_loc = mat.to_translation()
            new_mat = rot @ mat
            new_mat.translation = head_loc  # 保持肘关节位置不变，只改朝向
            pl.matrix = new_mat
            context.view_layer.update()
            return angle_before

        straightened = []
        for _u, _l in (("left_upper_arm", "left_lower_arm"),
                       ("right_upper_arm", "right_lower_arm")):
            a = _straighten_elbow(arm_bones.get(_u, ""), arm_bones.get(_l, ""))
            if a is not None:
                straightened.append(f"{arm_bones[_l]}({a:.1f}°)")
        if straightened:
            self.report({'INFO'}, "已自动修正肘部弯曲: " + ", ".join(straightened))

        # 10. 应用第二个修改器（复制的修改器）来调整网格姿态
        try:
            for mesh_obj in meshes_with_armature:
                context.view_layer.objects.active = mesh_obj
                bpy.ops.object.mode_set(mode='OBJECT')
                for modifier in mesh_obj.modifiers:
                    if modifier.type == 'ARMATURE' and modifier.object == obj and "_copy" in modifier.name:
                        bpy.ops.object.modifier_apply(modifier=modifier.name)
                        break
                if mesh_obj.name in shape_key_backups:
                    restore_shape_keys(mesh_obj, shape_key_backups[mesh_obj.name])
        except RuntimeError as e:
            self.report({'ERROR'}, f"应用修改器时出错：{str(e)}")
            return {'CANCELLED'}

        # 11. 切换回骨骼对象
        context.view_layer.objects.active = obj
        bpy.ops.object.mode_set(mode='POSE')

        # 12. 应用当前姿态为新的静置姿态
        bpy.ops.pose.armature_apply()

        # 13. 清理临时创建的网格
        for mesh_obj in meshes_with_armature:
            if mesh_obj.get("is_temp_mesh"):
                bpy.data.objects.remove(mesh_obj, do_unlink=True)

        self.report({'INFO'}, f"已完成A-Pose转换并应用为新的静置姿态")
        return {'FINISHED'}