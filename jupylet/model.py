"""
    jupylet/model.py
    
    Copyright (c) 2020, Nir Aides - nir@winpdb.org

    Redistribution and use in source and binary forms, with or without
    modification, are permitted provided that the following conditions are met:

    1. Redistributions of source code must retain the above copyright notice, this
       list of conditions and the following disclaimer.
    2. Redistributions in binary form must reproduce the above copyright notice,
       this list of conditions and the following disclaimer in the documentation
       and/or other materials provided with the distribution.

    THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
    ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
    WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
    DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR
    ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
    (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
    LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
    ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
    (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
    SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""


# Search pattern for gl instances: shadow(?!s|map_pass)

import moderngl
import logging
import weakref
import glob
import math
import glm

import PIL.Image

import moderngl_window.geometry as geometry
import numpy as np

from .lru import _lru_textures, _lru_materials, _MAX_TEXTURES, _MAX_MATERIALS
from .lru import SKYBOX_TEXTURE_UNIT, SHADOW_TEXTURE_UNIT
from .node import Object, Node
from .resource import get_shader_3d, pil_from_texture, get_context
from .resource import find_glob_path, unresolve_path, load_texture_cube


logger = logging.getLogger(__name__)


class ShadowMap(object):

    def __init__(self, size=1024, pad=12):

        self.size = size
        self.pad = pad
        
        self.layers = 0
        self.layer = 0

        self.tex = None
        self.smp = None
        self.fbo = None

    def __del__(self):
        self.release()

    def release(self):

        if self.tex is not None:
            self.tex.release()
            self.smp.release()
            self.fbo.release()

    def allocate(self, ctx, layers=8):
        
        self.layer = 0

        layers = int(max(4, 2 ** math.ceil(math.log2(layers))))
        if layers in (self.layers, self.layers // 2):
            return layers

        self.layers = layers

        self.release()

        self.tex = ctx.depth_texture((layers * self.size, self.size))
        self.smp = ctx.sampler(border_color=(1., 1., 1., 1.))
        
        self.fbo = ctx.framebuffer(depth_attachment=self.tex)
        self.fbo.clear()

    def use(self, location):

        self.tex.use(location=location)
        self.smp.use(location=location)

    def next_layer(self):

        self.fbo.viewport = (
            self.pad + self.layer * self.size,
            self.pad,
            self.size - 2 * self.pad,
            self.size - 2 * self.pad,
        )
        self.fbo.clear(viewport=self.fbo.viewport)

        self.layer += 1
        return self.layer - 1


class Scene(Object):
    
    def __init__(self, name, shadows=False):
        
        super(Scene, self).__init__()

        self.name = name
        
        self.meshes = {}
        self.lights = {}
        self.cameras = {}
        self.materials = {}
        
        self.shadows = shadows
        self.shadowmap = ShadowMap()

        self.skybox = None

    def add_material(self, material):
        self.materials[material.name] = material
        
    def add_mesh(self, mesh):
        self.meshes[mesh.name] = mesh
        
    def add_light(self, light):
        light.set_index(len(self.lights))
        self.lights[light.name] = light
        
    def add_camera(self, camera):
        self.cameras[camera.name] = camera
        
    def draw(self, shader=None):
        
        ctx = get_context()
        ctx.enable_only(moderngl.BLEND | moderngl.DEPTH_TEST | moderngl.CULL_FACE)

        shader = shader or get_shader_3d()

        shader._members['nlights'].value = len(self.lights)
        
        if self.shadows and self.shadowmaps_count:
            self.render_shadowmaps(shader)            
            self.shadowmap.use(location=SHADOW_TEXTURE_UNIT)
            shader._members['shadowmap_pass'].value = 2 

        else:
            shader._members['shadowmap_pass'].value = 0

        for light in self.lights.values():
            light.set_state(shader, self.shadows)

        for camera in self.cameras.values():
            camera.set_state(shader)

        for mesh in self.meshes.values():
            mesh.draw(shader)

        if self.skybox is not None:
            self.skybox.draw(shader)

    @property
    def shadowmaps_count(self):
        return sum(l.shadowmaps_count for l in self.lights.values() if l.shadows)

    def render_shadowmaps(self, shader):
    
        ctx = get_context()
        ctx.disable(moderngl.CULL_FACE)
        fb0 = ctx.fbo

        width, height = fb0.size 

        camera = list(self.cameras.values())[0]
        camera_position = glm.vec4(camera.position, 1.0)

        zfar = camera.zfar
        yfov = math.sin(camera.yfov) * zfar
        xfov = yfov * width / height

        camera_screen = camera._matrix * glm.mat4(
            xfov, yfov, -zfar, 1., 
            -xfov, yfov, -zfar, 1., 
            -xfov, -yfov, -zfar, 1., 
            xfov, -yfov, -zfar, 1.
        )

        shader.extra['shadowmap_pass'] = 1
        shader._members['shadowmap_pass'].value = 1
        
        self.shadowmap.allocate(ctx, self.shadowmaps_count)
        self.shadowmap.fbo.use()
        self.shadowmap.use(location=SHADOW_TEXTURE_UNIT)

        shader._members['shadowmap_texture'].value = SHADOW_TEXTURE_UNIT
        shader._members['shadowmap_layers'].value = self.shadowmap.layers
        shader._members['shadowmap_size'].value = self.shadowmap.size
        shader._members['shadowmap_pad'].value = self.shadowmap.pad
        
        for i, light in enumerate(self.lights.values()):

            if light.shadows:
    
                shader._members['shadowmap_light'].value = i

                for j in range(light.shadowmaps_count):

                    l = self.shadowmap.next_layer()

                    light.set_state_render_shadowmap(
                        j, l, shader, camera_position, camera_screen
                    )

                    for mesh in self.meshes.values():
                        mesh.draw(shader)
        
        shader.extra['shadowmap_pass'] = 0

        fb0.use()
        ctx.enable(moderngl.CULL_FACE)
    

class Material(Object):
    
    def __init__(
        self,
        name,
        color,
        metallic=0,
        roughness=0.5,
        specular=0.1,
        emissive=[0, 0, 0],
        occlusion=None,
        normals=None,
        normals_scale=1,
    ):
        
        super(Material, self).__init__()
        
        self.name = name

        self._items = dict(
            color = color,
            metallic = metallic,
            roughness = roughness,     
            specular = specular,     
            emissive = emissive,
            occlusion = occlusion,
            normals = normals,
            normals_scale = normals_scale or 1,
            normals_gamma = self.compute_normals_gamma(normals),
        )

        self._mlid, self._mslot = _lru_materials.allocate()[1:3]
        self._clid, self._cslot = self.allocate_texture(self._items['color'])[1:3]
        self._nlid, self._nslot = self.allocate_texture(self._items['normals'])[1:3]
        self._elid, self._eslot = self.allocate_texture(self._items['emissive'])[1:3]
        self._rlid, self._rslot = self.allocate_texture(self._items['roughness'])[1:3]
        
    def allocate_texture(self, t, lid=None):
        if isinstance(t, moderngl.texture.Texture):
            return _lru_textures.allocate(lid)
        return None, None, None, None

    def compute_normals_gamma(self, normals):

        if not isinstance(normals, moderngl.texture.Texture):
            return 1.

        na = np.array(pil_from_texture(normals))
        nm = na[...,:2].mean() / 255

        return math.log(0.5) / math.log(nm)        

    def set_state(self, shader):
            
        ctx = get_context()

        shader._members['material'].value = self._mslot

        _, _, self._mslot, mnew = _lru_materials.allocate(self._mlid)
        _, _, self._cslot, cnew = self.allocate_texture(self._items['color'], self._clid)
        _, _, self._nslot, nnew = self.allocate_texture(self._items['normals'], self._nlid)
        _, _, self._eslot, enew = self.allocate_texture(self._items['emissive'], self._elid)
        _, _, self._rslot, rnew = self.allocate_texture(self._items['roughness'], self._rlid)

        dirty = self._dirty or mnew or cnew or nnew or enew or rnew

        if dirty:

            self._dirty.clear()

            material = 'materials[%s].' % self._mslot

            if isinstance(self.color, moderngl.texture.Texture):
                
                ctx.clear_samplers(self._cslot, self._cslot+1)
                self.color.use(location=self._cslot)
                
                shader._members['textures[%s].t' % self._cslot].value = self._cslot
                shader._members[material + 'color_texture'].value = self._cslot
            else:
                shader._members[material + 'color_texture'].value = -1
                shader._members[material + 'color'].value = tuple(self.color)
                    
            if isinstance(self.normals, moderngl.texture.Texture):
                
                shader._members[material + 'normals_scale'].value = self.normals_scale
                shader._members[material + 'normals_gamma'].value = self.normals_gamma

                ctx.clear_samplers(self._nslot, self._nslot+1)
                self.normals.use(location=self._nslot)
                
                shader._members['textures[%s].t' % self._nslot].value = self._nslot
                shader._members[material + 'normals_texture'].value = self._nslot
            else:
                shader._members[material + 'normals_texture'].value = -1
                    
            if isinstance(self.emissive, moderngl.texture.Texture):
                
                ctx.clear_samplers(self._eslot, self._eslot+1)
                self.emissive.use(location=self._eslot)
                
                shader._members['textures[%s].t' % self._eslot].value = self._eslot
                shader._members[material + 'emissive_texture'].value = self._eslot
            else:
                shader._members[material + 'emissive_texture'].value = -1
                shader._members[material + 'emissive'].value = tuple(self.emissive)

            if isinstance(self.roughness, moderngl.texture.Texture):
                
                ctx.clear_samplers(self._rslot, self._rslot+1)
                self.roughness.use(location=self._rslot)
                
                shader._members['textures[%s].t' % self._rslot].value = self._rslot
                shader._members[material + 'roughness_texture'].value = self._rslot
            else:
                shader._members[material + 'roughness_texture'].value = -1
                shader._members[material + 'roughness'].value = self.roughness
                shader._members[material + 'metallic'].value = self.metallic
                    
            shader._members[material + 'specular'].value = self.specular
        

LIGHT_TYPE = {
    'directional': 0,
    'point': 1,
    'spot': 2,
}


class Light(Node):
    
    def __init__(
        self,       
        name,
        rotation=None, 
        scale=None, 
        position=None,
        type='point',
        color=glm.vec3(1.),
        intensity=500,
        ambient=0.001,
        outer_cone=math.pi/4,
        inner_cone=math.pi/4 * 0.9,
        shadows=True,
        **kwargs
    ):
        
        super(Light, self).__init__(name, None, scale, rotation, position)
        
        self.index = -1
        
        self._items = dict(

            type = type,
            
            color = [round(c, 3) for c in color],
            intensity = round(intensity, 3),
            ambient = ambient,

            swidth = 32.,
            snear = 0.01,
            sfar = 100.,

            inner_cone = inner_cone,
            outer_cone = outer_cone,

            pcf = 3,
            bias = 0.005,
            shadows = shadows,
        )

        self.shadowmaps_depths = [1.0, 0.6, 0.3, 0.1, 0.]
        
    @property
    def shadowmaps_count(self):
        if self._items['type'] == 'directional':
            return len(self.shadowmaps_depths) - 1
        else:
            return 1
 
    def set_index(self, index):  
        self.index = index

    def get_uniform_name(self, key):
        return 'lights[%s].%s' % (self.index, key)
 
    def set_state(self, shader, shadows):
        
        _trigger_dirty_flat = self.matrix

        if self._dirty:

            self._dirty.clear()
                            
            prefix = self.get_uniform_name('')

            shader._members[prefix + 'type'].value = LIGHT_TYPE[self.type]
            shader._members[prefix + 'color'].value = tuple(self.color)
            shader._members[prefix + 'intensity'].value = self.intensity
            shader._members[prefix + 'ambient'].value = self.ambient
            
            shader._members[prefix + 'position'].value = tuple(self.position)
            shader._members[prefix + 'direction'].value = tuple(self.front)
            
            shader._members[prefix + 'inner_cone'].value = math.cos(self.inner_cone)
            shader._members[prefix + 'outer_cone'].value = math.cos(self.outer_cone)

            shader._members[prefix + 'snear'].value = self.snear

            shader._members[prefix + 'shadows'].value = self.shadows

            shader._members[prefix + 'shadowmap_pcf'].value = self.pcf
            shader._members[prefix + 'shadowmap_bias'].value = self.bias
            shader._members[prefix + 'shadowmap_textures_count'].value = self.shadowmaps_count
                        
    def set_state_render_shadowmap(
        self, 
        shadowmap_index, 
        shadowmap_layer, 
        shader, 
        camera_position, 
        camera_screen
    ):
        
        si = shadowmap_index

        prefix = self.get_uniform_name('')

        view = glm.lookAt(self.position, self.position - self.front, self.up)

        if self.type == 'directional':
            
            position = view * camera_position
            screen = view * camera_screen

            min0, max0 = compute_plane_minmax(
                position, screen, self.shadowmaps_depths[si]
            )

            min1, max1 = compute_plane_minmax(
                position, screen, self.shadowmaps_depths[si+1]
            )

            min2 = glm.min(min0, min1)
            max2 = glm.max(max0, max1)

            projection = glm.ortho(
                round(min2.x), 
                round(max2.x), 
                round(min2.y), 
                round(max2.y), 
                self.snear, 
                self.sfar
            )
            
            scale = max(max2.x - min2.x, max2.y - min2.y)
            shader._members[prefix + 'scale'].value = scale

        elif self.type == 'point':
            projection = glm.perspective(
                math.pi / 2, 
                1., 
                self.snear, 
                self.sfar
            )

        else:
            projection = glm.perspective(
                2 * self.outer_cone, 
                1., 
                self.snear, 
                self.sfar
            )
        
        self._view = view
        self._proj = projection
        
        pv = projection * view

        shader._members[prefix + 'shadowmap_textures[%s].layer' % si].value = shadowmap_layer
        shader._members[prefix + 'shadowmap_textures[%s].depth' % si].value = self.shadowmaps_depths[si]
        shader._members[prefix + 'shadowmap_textures[%s].projection' % si].write(pv)
        shader._members[prefix + 'shadowmap_projection'].write(pv)


def compute_plane_minmax(position, far_screen, split):
    
    pm = glm.mat4(position, position, position, position)
    s0 = (far_screen - pm) * split + pm
    
    min0 = glm.min(s0[0], s0[1], s0[2], s0[3])
    max0 = glm.max(s0[0], s0[1], s0[2], s0[3])
    
    return min0, max0


class Camera(Node):
    
    def __init__(
        self, 
        name, 
        rotation=None, 
        scale=None, 
        position=None,
        type='perspective',
        znear=100,
        zfar=100,
        yfov=glm.radians(60),
        xmag=1.,
        ymag=1.,
        **kwargs
    ):
        
        super(Camera, self).__init__(name, None, scale, rotation, position)
        
        self._items = dict(
            type = type,
            znear = round(znear, 3),
            zfar = round(zfar, 3),
            yfov = round(yfov, 3),
            xmag = round(xmag, 3),
            ymag = round(ymag, 3),
        )

        self._aspect = 0

    def set_state(self, shader):

        width, height = get_context().fbo.size

        dirty = self._aspect != width / height
        self._aspect = width / height
                    
        _trigger_dirty_flat = self.matrix

        if dirty or self._dirty:

            self._view0 = glm.lookAt(self.position, self.position - self.front, self.up)
            self._proj0 = glm.perspective(
                self.yfov, 
                self._aspect, 
                self.znear, 
                self.zfar
            )

            shader._members['view'].write(self._view0)
            shader._members['camera.position'].value = tuple(self.position)

            shader._members['projection'].write(self._proj0)
            shader._members['camera.zfar'].value = self.zfar

            self._dirty.clear()

    
class Mesh(Node):
    
    def __init__(self, name, rotation=None, scale=None, position=None, parent=None):
        
        super(Mesh, self).__init__(name, None, scale, rotation, position)
        
        self.primitives = []
        self.children = {}
        
        self.shadow_bias = 0
        self.hide = False
        
        self._parent = weakref.proxy(parent) if parent else None

    def add_primitive(self, primitive):
        self.primitives.append(primitive)
        
    def composed_matrix(self):
        
        if self._parent is None:
            return self.matrix

        return self._parent.composed_matrix() * self.matrix 

    def draw(self, shader):
        #logger.debug('Enter Mesh.draw(shader=%r).', shader)

        if self.hide:
            return 

        shader._members['mesh_shadow_bias'].value = self.shadow_bias
        shader._members['model'].write(self.composed_matrix())
        
        for p in self.primitives:
            p.draw(shader)

        for c in self.children.values():
            c.draw(shader)

    
class Primitive(Object):
    
    def __init__(self, material, indices, vertices, normals=None, coords=None):
        
        super(Primitive, self).__init__()
        
        self.material = material
        self.nvertices = len(vertices[0]) // 3
        self.content = []
        self.has_uvs = coords is not None
        self.shader = None
        self.vao = None

        ctx = get_context()

        self.indices = ctx.buffer(indices.astype('i4'))

        if vertices: 
            data, fmt = vertices
            self.content.append((ctx.buffer(data), fmt, 'in_position'))
        
        if normals:
            data, fmt = normals
            self.content.append((ctx.buffer(data), fmt, 'in_normal'))

        if coords:
            data, fmt = coords
            self.content.append((ctx.buffer(data), fmt, 'in_texcoord_0'))

    def __del__(self):

        if self.vao is not None:
            self.vao.release()

        self.indices.release()

        for b, f, n in self.content:
            b.release()

    def draw(self, shader):
        #logger.debug('Enter Primitive.draw(shader=%r).', shader)

        if shader.extra.get('shadowmap_pass') != 1:
            self.material.set_state(shader)

        vao = self.get_vao(shader)
        vao.render()

    def get_vao(self, shader):

        if self.shader is not shader:
            self.shader = shader
            if self.vao:
                self.vao.release()

            ctx = get_context()
            self.vao = ctx.vertex_array(
                self.shader, 
                self.content, 
                self.indices
            )

        return self.vao
        

class Skybox(Object):
    
    def __init__(self, path, flip=False, flip_left_right=False, intensity=1.0):
        
        super(Skybox, self).__init__()
        
        ctx = get_context()

        self.path = path
        #self.smpl = ctx.sampler(compare_func='<=')
        self.cube = geometry.cube(size=(1, 1, 1))
        self.hide = False
                
        self._items = dict(
            intensity = intensity,
            texture = load_texture_cube(
                path, 
                flip=flip, 
                flip_left_right=flip_left_right
            ),
        )

    def draw(self, shader=None):
                
        if self.hide:
            return 

        ctx = get_context()

        ccw = ctx.front_face
        ctx.front_face = 'cw'
        ctx.depth_func = '<='

        shader = shader or get_shader_3d()
        shader._members['skybox.render_skybox'].value = 1

        if self._dirty:
            
            shader._members['skybox.intensity'].value = self.intensity
            shader._members['skybox.texture_exists'].value = 1
            shader._members['skybox.texture'].value = SKYBOX_TEXTURE_UNIT

            self.texture.use(location=SKYBOX_TEXTURE_UNIT)

            self._dirty.clear()

        self.cube.render(shader)

        shader._members['skybox.render_skybox'].value = 0

        ctx.depth_func = '<'
        ctx.front_face = ccw

